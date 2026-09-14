# PATCH v0.2 章节 → 代码映射表

把 `PATCH_v0.2_即时演出与持久认知分离_移除本地2B核心依赖.md` 的每一节（§0–§33）映射到本仓库中的**代码位置 / 测试文件 / 实现状态**。

**本文只记录事实。未实现的条目一律显式标注，不做"看起来差不多"的推测。**

| 项目 | 值 |
|---|---|
| 补丁文件 | `F:\理解痞老板\PATCH_v0.2_即时演出与持久认知分离_移除本地2B核心依赖.md`（1352 行，§0–§33） |
| 代码根 | `runtime/src/companion_runtime/` |
| 测试根 | `runtime/tests/` |
| 核对时的 git 修订 | `15b39d7`（`feat(runtime): split acting layer from persistent cognition per PATCH v0.2`） |
| 核对时的测试规模 | `python -m pytest -q` 收集 559 项，全部通过（数量随模块演进增长，以实际运行为准） |
| 核对方式 | 直接阅读源码 + 运行测试 + `git status` / `git diff` 比对 |

> **关于工作区状态的提醒。** 核对时 `reducer.py` 与 `typing.py` 中与深层刷新落地相关的改动**尚未提交**（`git status` 显示为 `M`）。因此下表中"Reducer 侧 deep_refresh 处理器"一行标注为"已实现（工作区，未提交）"。提交状态可随时用 `git log -1 --stat` 与 `git status` 复核。

## 状态图例

| 状态 | 含义 |
|---|---|
| **已实现** | 代码在位，有测试，且**存在生产调用方**（即真的会在运行时生效） |
| **部分实现** | 代码在位但链路不完整：缺调用方、缺上游生产者、或只覆盖了补丁要求的一部分 |
| **未实现** | 只有配置项 / 文档 / 注释，没有任何生产代码消费它 |
| **文档/约定** | 补丁这一节本身是论证、哲学或运维结论，没有对应代码可写；落点在 README 或代码注释 |

---

## 一、逐章节映射

| 补丁章节 | 主题 | 代码位置 | 测试 | 状态 |
|---|---|---|---|---|
| §0 | 补丁摘要：双层时间模型 | `semantic.py`（模块 docstring）、`runtime.py::Runtime.process_user_message`（结算段注释）、`context.py::PRIORITY_PREAMBLE` | `test_acting_layer_independence.py` | **已实现** |
| §1 | 为什么重写上一版：不只是"2B 太慢" | `providers.py`（本地模型降级为可选实现）、`local_llm.py`（保留但仅被 `providers.py` 引用） | `test_providers.py`、`test_local_llm.py` | **已实现** |
| §2 | 实测仍然构成硬约束 | 无代码；落点在 `README.md` 第 15.2 节 | — | **文档/约定** |
| §3 | 新的双层时间模型 | `context.py`（区块划分）、`semantic.py`、`runtime.py` | `test_acting_layer_independence.py::TestTwoTimeScaleContract` | **已实现** |
| §4 | 即时演出层（由主 LLM 负责） | 宿主侧主 LLM；Runtime 侧只负责不做（`runtime.py::MessageOutcome` 记录本轮结算结论） | `test_api.py`、`test_acting_layer_independence.py` | **已实现** |
| §4.1 | 主 LLM 本来就拥有即时理解能力 | `context.py::PRIORITY_PREAMBLE`、`context.py::SECTION_PSYCH`（"进入本轮前的长期状态（背景）"） | `test_acting_layer_independence.py::test_context_block_is_marked_as_background` | **已实现** |
| §4.2 | 即时演出 ≠ 持久状态写入 | `test_integration_scenarios.py` 不变量 6、`runtime.py` 中 `appraisal_source` 的 `coarse_rule / deferred` 语义 | `test_invariant_6_main_llm_has_no_state_write_authority` | **已实现** |
| §5 | 持久认知层 | `emotion.py`、`memory.py`、`unfinished.py`、`user_model.py`、`candidate.py`、`motivation.py` | 各模块对应测试文件 | **已实现** |
| §5.1 | Runtime 目标从"即时理解"改为"长期连续性" | `semantic.py::potential_relevance`、`projections.py::SemanticProjection` | `test_semantic.py::TestRelevancePrioritisation` | **已实现** |
| §6 | 新的完整当前轮流程 | `runtime.py::Runtime.process_user_message`（`lazy_tick` → 事件落库 → 边界 → 工作局势 → 粗粒度结算） | `test_acting_layer_independence.py` | **已实现** |
| §7 | 当前消息与旧 Runtime 心理状态的优先级 | `context.py::PRIORITY_PREAMBLE` | `test_delivery_scheduler_context.py`、`test_acting_layer_independence.py` | **部分实现** — preamble 只写了 5 级：`宿主设定与安全约束 > 当前用户原话 > 当前确定事实 > 显式边界 > 长期状态`；补丁的第 5–6 级（Runtime 持久心理状态、心理解释缓存）与第 7 级（主 LLM 自然发挥）被合并成一句"这里的长期状态"，未逐级进入 prompt 文本 |
| §7.1 | 优先级示例（旧缓存说"失落"，新消息说"回来陪你"） | 无专门代码：这是主 LLM 的行为约定，Runtime 只提供背景 | 无专门测试 | **部分实现** — 机制在（preamble 声明"以当前用户原话为准"），但"旧缓存不得压制当前原话"没有可断言的代码路径，只能靠 prompt 约定 |
| §8 | "即时反应"与"情绪余波"正式分离 | `runtime.py`（结算 → `apply_new_emotion_events`；未结算则不产生余波） | `test_ambiguous_events_are_deferred_not_guessed` | **已实现** |
| §9 | 移除"每轮精确情绪评价"的要求 | `semantic.py::classify_event` 的 `None` 路径 + `MessageOutcome.appraisal_source == "deferred"` | `test_semantic.py::TestAmbiguousEventsAreNotSettled` | **已实现** |
| §10 | 粗粒度持久情绪更新（八类方向，不命名情绪） | `semantic.py::ANCHORS`、`semantic.py::_BAND_INTENSITY`、`semantic.py::settlement_to_evaluation` | `test_semantic.py::TestExplicitAnchorsSettle`、`TestAnchorTableIntegrity` | **已实现** — 补丁的八类方向都有锚点覆盖：明确正向（`explicit_positive_feedback` / `explicit_joy`）、明确负向（`explicit_distress`）、明确拒绝（`explicit_refusal`）、明确感谢（`explicit_positive_feedback`）、明确冲突（`explicit_conflict`）、明确和解（`explicit_repair`）、明确边界（`explicit_need_for_space`）、明显重大事件（`major_loss` / `major_setback`） |
| §10.1 | 示例 `{direction, impact, source}` | `semantic.py::CoarseSettlement.to_dict` | `test_semantic.py::test_anchor_settles_with_the_expected_reading` | **已实现（字段名不同）** — 实现输出 `direction / intensity / confidence / source / evidence / semantic_label`（强度用 band 名而不是 `impact: "medium_high"` 的键名），语义等价；`semantic_label` 恒为 `None` |
| §11 | 模糊事件允许保持未解释 | `semantic.py::classify_event` 歧义否决 + `projections.py::SemanticProjection.record_unresolved` + `semantic.py::UnresolvedRecord` | `test_semantic.py::test_exact_patch_example_is_unresolved`、`test_every_ambiguity_marker_is_covered_by_a_negative_case`、`test_acting_layer_independence.py::test_raw_event_survives_being_unresolved` | **已实现** — 「算了，也没什么。」被点名为必须 unresolved，且有直接断言 |
| §12 | 延迟理解从兜底升级为正式特性 | `semantic.py::SemanticStatus.UNRESOLVED`、`event_semantics.unresolved_reason`、`semantic.py::resolve_backlog` | `test_acting_layer_independence.py::test_unresolved_events_accumulate_for_a_later_refresh` | **部分实现** — "记下来"和"可以延迟"已实现且是一等公民；"之后再结算"的自动化路径未接通（见 §18–§21）：`resolve_backlog()` 目前**无生产调用方** |
| §13 | "追夫火葬场"：允许错过当下，但不能丢失原始证据 | `eventlog.py`（append-only，不变量 1）、`event_semantics` 表、`reducer.py::Reducer._apply_reinterpretation`（新版本 + `reappraisals`，从不回写旧事件） | `test_invariant_1_raw_events_are_never_modified`、`test_invariant_7_reinterpretation_never_rewrites_the_past` | **部分实现** — 证据保全与"追加式重解释"机制齐备；**自动触发**的重解释依赖尚未实现的深层刷新链。手工路径可用：`POST /proposals {task_type:"deep_refresh", payload:{operations:[{kind:"reinterpretation",...}]}}` |
| §14 | 心理解释不再是高频必需品 | `emotion.py::EmotionExplainer._render`（provider 优先，拿不到就 `_render_template`） | `test_emotion_boundaries_unfinished.py`、`test_providers.py::test_explain_state_success_and_template_fallback` | **已实现** |
| §14.1 | 模板只负责长期底色 | `emotion.py::TEMPLATES_POSITIVE / TEMPLATES_NEGATIVE / TEMPLATES_NEUTRAL`、`emotion.py::EmotionExplainer._render_template`、`context.py::SECTION_PSYCH` | `test_delivery_scheduler_context.py`、`test_acting_layer_independence.py::test_context_block_is_marked_as_background` | **已实现** — 模板措辞全部改成"进入本轮之前…的底色"口径；测试断言注入块含"背景"与"当前" |
| §15 | 深层心理解释变成缓存 | `db.py::emotion_explanations`、`projections.py`（`store_explanation` / `cached_explanation`）、`emotion.py::EmotionExplainer.explain_and_store`、`reducer.py::Reducer._apply_interpretation_cache` | `test_emotion_boundaries_unfinished.py`（解释器缓存）、`test_providers.py`（provider 侧 `state_key` 缓存） | **部分实现** — 缓存、复用、`source="deep_refresh"` 写入都在位；补丁要求的 stale 判据（"背景心境明显变化 / 主要活跃事件变化 / 重大重估事件出现"）目前只有 TTL 近似：`task.explain_cache_ttl_seconds = 1800`；`semantic.interpretation_max_age_seconds = 21600` **未被任何生产代码读取** |
| §16 | 本地 2B 正式从标准架构移除 | `local_llm.py` 保留，但**只被 `providers.py` 引用**；`Runtime` 不 import 它；`config.SemanticConfig.provider` 默认 `"disabled"` | `test_acting_layer_independence.py::test_ingest_works_with_no_provider_configured`、`test_no_outbound_socket_is_opened_during_ingest` | **已实现** |
| §17 | 可选 Semantic Provider | `providers.py`：`SemanticProvider` / `DisabledProvider` / `LocalCPUProvider` / `LocalGPUProvider` / `RemoteAPIProvider` / `build_provider` | `test_providers.py`（73 项，含四种实现选择、回落、fail-open、密钥卫生） | **已实现** |
| §18 | 强语义模型的新职责：深层认知刷新 | `providers.py::DeepRefreshRequest`、`DeepRefreshSuggestions`、`DEEP_REFRESH_SYSTEM_PROMPT`、`_OpenAICompatibleProvider.deep_refresh`；落地端 `reducer.py::Reducer._apply_deep_refresh` | `test_providers.py` | **部分实现** — 端口、契约、prompt、四种 provider 的 `deep_refresh()` 与 Reducer 落地端都在位；**触发与编排不存在**（见下） |
| §19 | 深层认知刷新输入 | `providers.py::DeepRefreshRequest`（`unresolved_events` / `situation` / `mood` / `active_emotions` / `memories` / `unfinished` / `user_model_summary` / `candidates` / `key_quotes`，9 个字段与补丁清单一致） | `test_providers.py` | **部分实现** — 结构已定义且有测试，但**没有任何生产者**组装它 |
| §20 | 输出建议集（六个字段，只有建议权） | `providers.py::DEEP_REFRESH_FIELDS`、`parse_deep_refresh`、`DeepRefreshSuggestions`；`reducer.py::Reducer._apply_deep_refresh`（`reinterpretation` / `psychological_interpretation` / `candidate_intent` / `memory` / `unfinished_matter` / `user_model_evidence` 六种 `kind`） | `test_providers.py`（逐字段类型校验、部分畸形、`suggestions` 包装键） | **部分实现（工作区，未提交）** — 六个字段与六种落地 `kind` 一一对应且都过 Reducer；缺的是"建议 → grounded operation"的转换层 |
| §21 | 深层认知刷新何时触发 | `config.py::SemanticConfig`：`deep_refresh_enabled` / `unresolved_backlog_threshold` / `unresolved_max_age_hours` / `deep_refresh_min_interval_seconds` / `deep_refresh_idle_hours` / `max_operations_per_refresh` | 无 | **未实现** — 六个旋钮全部**没有生产消费者**（`grep` 仅在 `config.py` 命中） |
| §22 | 新架构三级结构 | Level 0：`runtime.py` / `motivation.py` / `boundaries.py` / `unfinished.py` / `protocol.py`；Level 1：`semantic.py` / `emotion.py` / `memory.py` / `user_model.py`；Level 2：`providers.py` | 各模块测试 | **部分实现** — Level 0 与 Level 1 完整；Level 2 只有端口与落地端，中间的编排缺失 |
| §23 | 新总流程 | `runtime.py::Runtime.process_user_message`（走到"不可理解部分进入 unresolved"） | `test_acting_layer_independence.py` | **部分实现** — 流程在"进入 unresolved"处为止；后半段"必要时低频深层认知刷新 → 影响未来轮次"未实现 |
| §24 | 主 LLM 与 Runtime 的新权力边界 | `reducer.py`（唯一写者）、`protocol.py`（APPLY/REBASE/DISCARD）、不变量 3 与 6 | `test_invariant_3_background_models_never_write_directly`、`test_invariant_6_main_llm_has_no_state_write_authority` | **已实现** |
| §25 | 最重要的新设计原则 | 文档 + 代码注释（`semantic.py` 模块 docstring、`runtime.py` 结算段注释、`providers.py` 三条契约） | — | **文档/约定** |
| §26 | 对"情绪模块"的重新定义 | `emotion.py`（数值动力学 + 长期底色）+ `semantic.py`（粗粒度方向/强度/时间/来源） | `test_semantic.py`、`test_emotion_boundaries_unfinished.py` | **已实现** |
| §27 | 对"情绪解释器"的重新定义（低频语义压缩器） | `emotion.py::EmotionExplainer`（模板兜底）+ `providers.py::explain_state` + `reducer.py::Reducer._apply_interpretation_cache` | `test_providers.py`（含 `state_key` 缓存契约） | **部分实现** — 解释器与模板都在位，`explain_state()` 也有完整实现和测试；但 `context.py::runtime_explanation` 与 `api.py` 的 `POST /explain` 构造 `EmotionExplainer` 时**没有传入 provider**，因此生产路径永远不会调用 `explain_state()` |
| §28 | 关键路径预算的最终理解 | `runtime.py::Runtime.process_user_message` 不含任何模型调用 | `test_no_outbound_socket_is_opened_during_ingest`（直接拦 `socket.connect`） | **已实现** |
| §29 | 对弱 VPS 的最终意义 | 无代码；落点在 `README.md` 第 15 节（含实测 CPU 成本量级） | — | **文档/约定** |
| §30 | 旧流程与新流程对比 | 无代码；落点在 `README.md` 第 0 节与第 9.2 节 | — | **文档/约定** |
| §31 | 典型例子：用户说"算了，也没什么" | `semantic.py::AMBIGUITY_MARKERS`（含 `算了` / `也没什么`）、`classify_event`、`record_unresolved` | `test_exact_patch_example_is_unresolved`、`test_ambiguous_events_are_deferred_not_guessed`、`test_raw_event_survives_being_unresolved`、`test_unresolved_events_do_not_leak_into_the_block_as_facts` | **部分实现** — 当前轮与"不瞎猜、不丢证据"已完整实现并有直接断言；例子后半段（"后来用户说'你果然当时没发现' → 深层刷新重新解释 → 生成 `reappraisal_event`"）依赖尚未实现的刷新链 |
| §32 | 最终架构哲学修正 | `semantic.py` / `providers.py` 的模块 docstring、`README.md` 第 0 节与第 5 节 | — | **文档/约定** |
| §33 | 最终结论 | `providers.py::build_provider`（默认回落 `DisabledProvider`）、`config.py::SemanticConfig.provider = "disabled"`、`local_llm.py` 不在任何生产调用链上 | `test_providers.py`、`test_acting_layer_independence.py` | **已实现** |

---

## 二、必须诚实标注为「未实现」的条目

以下每一条都经过源码核对：**没有任何生产代码路径会执行它**（只有定义、配置、注释或测试）。

| # | 未实现的东西 | 证据 | 影响 |
|---|---|---|---|
| 1 | **深层认知刷新的触发与编排**：没有任何生产代码组装 `DeepRefreshRequest`、调用 `provider.deep_refresh()`，也没有把建议集 ground 成可应用操作的转换层 | `grep -r "deep_refresh"` 在 `src/` 只命中 `providers.py`（定义/实现）、`config.py`（配置字段）、`db.py`（列名）、`projections.py`（`settle_from_deep_refresh`）、`reducer.py`（落地端）、`typing.py`（枚举）；`runtime.py` / `scheduler.py` / `api.py` **零命中**；`reducer.py` 文档中引用的 `companion_runtime.deep_refresh` 模块**当前不存在**（`Test-Path src/companion_runtime/deep_refresh.py` = False） | "后来想明白"这条自动化路径不存在。unresolved 事件会一直挂着，直到有人手工经 `POST /proposals` 提交 grounded bundle |
| 2 | **`SemanticConfig` 的 6 个深层刷新旋钮无消费者**：`deep_refresh_enabled` / `unresolved_backlog_threshold` / `unresolved_max_age_hours` / `deep_refresh_min_interval_seconds` / `deep_refresh_idle_hours` / `max_operations_per_refresh` | `grep` 这些名字，除 `config.py` 的定义与 docstring 外**没有任何其他命中** | 改这些值目前不改变任何行为 |
| 3 | **`template_fallback` 与 `interpretation_max_age_seconds` 无消费者** | 同上（`grep` 仅命中 `config.py`） | 模板兜底当前无条件生效（不可关）；解释缓存的陈旧判定实际由 `task.explain_cache_ttl_seconds` 承担 |
| 4 | **`semantic.resolve_backlog()` 无生产调用方** | 仅 `test_semantic.py` 引用 | "把积压切成 live / stale"的能力存在但不能从运行时触发 |
| 5 | **`explain_state()` 未接线**：`context.py::runtime_explanation()` 与 `api.py` 的 `POST /explain` 都用 `EmotionExplainer(runtime.projections.emotion, runtime.config)` 构造，未传 provider | 直接读两处构造调用；`Runtime.__init__` 里构造出的 `runtime.semantic_provider` 只被 `api.py` 的 `/health` 读取 | 即使配了 `RemoteAPIProvider`，心理解释也**不会**真的走远端；总是模板/缓存 |
| 6 | **候选意图生成未接入 Reducer**：`DeepRefreshSuggestions.candidate_intent_operations` 只是模型建议列表；Reducer 只接受已经 ground 好的 `{"kind":"candidate_intent","payload":{...},"sources":[...]}` | `reducer.py::Reducer._apply_deep_refresh` 的 `candidate_intent` 分支直接读 `operation["payload"]` 交给候选池管理器；从建议到该形状的转换代码不存在 | 强语义模型目前**无法**自动往候选池里加念头；必须由外部调用方自己构造操作 |
| 7 | **无 HTTP 端点查看 unresolved 列表或手动触发刷新** | `api.py` 的全部 `@router` 装饰器清单：只有 `/health` 暴露 `semantics` 计数（`projections.semantics.stats()`），没有列表端点，也没有刷新端点 | 运维只能看到积压个数，看不到是哪几条 |
| 8 | **优先级未逐级进入 prompt**：补丁 §7 的 7 级链在 `PRIORITY_PREAMBLE` 里被压成 5 级 | `context.py::PRIORITY_PREAMBLE` 的字符串内容 | "Runtime 持久心理状态 > 心理解释缓存 > 主 LLM 自然发挥"这三级的相对顺序没有被显式声明 |
| 9 | **`deep_refresh` 无独立敏感度条目** | `protocol.py::TASK_SENSITIVITY` 无 `deep_refresh` 键；`sensitivity_of()` 回落到默认 `"medium"`（预算 6 版） | 深层刷新结果按 `medium` 预算 rebase，未针对"低频重理解"单独标定 |
| 10 | **自动后验重解释（`reappraisal_event`）未接通** | `reducer.py::Reducer._apply_reinterpretation` 已能写 `reappraisals`，但没有任何自动触发者 | 补丁 §12 / §13 / §31 描述的"几小时后突然意识到"不会自动发生 |
| 11 | **embedding 检索仍是词法降级** | `memory.py::MemoryStore.retrieve()` 是词面重合 + 结构化加权；接口是留给 embedding sidecar 的接缝 | 补丁 §29 提到的"可选轻量 embedding"不存在；语义相近但用词不同的记忆检索不到 |
| 12 | **无常驻巩固 worker** | 巩固由调用方驱动（`memory.consolidate()`），没有后台线程 | 与 v0.2 无关的既有边界，此处一并记录 |
| 13 | **无 Prometheus 指标导出** | 只有 `/health`、`/maintenance/verify`、`/outbox` 的结构化输出 | 运维需要自己抓 HTTP |

---

## 三、部分实现条目：「差在哪」一句话清单

| 补丁章节 | 差在哪 |
|---|---|
| §7 / §7.1 | preamble 只声明到"显式边界 > 长期状态"，未逐级区分"持久心理状态 / 解释缓存 / 主 LLM 自然发挥" |
| §10.1 | 输出字段名与补丁 JSON 示例不完全一致（`intensity` band 名 vs `impact`），语义等价 |
| §12 / §13 / §23 / §31 | "记下来 + 不丢证据"完整；"之后再自动结算/重解释"缺失（未实现 #1、#10） |
| §15 | 缓存与写入都在位；stale 判据用 TTL 近似，补丁要求的"心境/活跃事件/重大重估"三类事件驱动未被消费 |
| §18 / §19 / §20 / §22 | 端口 + 契约 + 解析 + 落地端齐备；**生产者、触发条件、grounding 转换层缺失**（未实现 #1、#2、#6） |
| §27 | `explain_state()` 实现完整且有测试，但生产路径未传 provider（未实现 #5） |

---

## 四、怎么自己复核这些结论

```powershell
cd F:\理解痞老板\runtime

# 1) 测试规模与状态（数字随开发变化，以你的运行为准）
python -m pytest -q

# 2) 深层刷新到底有没有生产调用方？（预期：src 内只有定义/配置/落地端）
Get-ChildItem -Recurse src -Filter *.py | Select-String "deep_refresh"

# 3) 补丁引用的编排模块是否存在？（核对时：不存在）
Test-Path src\companion_runtime\deep_refresh.py

# 4) SemanticConfig 的旋钮有没有被消费？（预期：除 config.py 外无命中）
Get-ChildItem -Recurse src -Filter *.py |
    Select-String "unresolved_backlog_threshold|deep_refresh_idle_hours|max_operations_per_refresh|template_fallback|interpretation_max_age_seconds"

# 5) explain_state 有没有被接上？（看这两处构造是否传了 provider）
Select-String -Path src\companion_runtime\context.py,src\companion_runtime\api.py -Pattern "EmotionExplainer\("

# 6) 补丁点名的那句话是否真的保持 unresolved？（预期：None）
$env:PYTHONPATH="src"; python -c "from companion_runtime.semantic import classify_event; print(classify_event('算了，也没什么。'))"

# 7) 工作区里哪些改动还没提交
git status --porcelain
```

---

## 五、一句话总结

补丁 v0.2 的**架构性结论已经落地并可验证**：Runtime 以零模型完整运行、入口路径不做任何模型调用（有拦 `socket.connect` 的结构性测试）、显式事件粗粒度结算、模糊事件诚实记为 `unresolved` 且永不丢失原始事件、心理解释退化为模板 + 缓存、心理解释器降级为可选。

**尚未落地的是一个环节：深层认知刷新的编排。** 端口（`SemanticProvider`）、契约（`DeepRefreshRequest` / `DeepRefreshSuggestions` / `parse_deep_refresh`）、落地端（`Reducer._apply_deep_refresh`）三件套都在位，但**中间那条"什么时候刷、拿什么去刷、把建议 ground 成什么操作"的链子还没接**。因此本仓库当前的状态是：**持久认知层能记住、能不猜、能不丢，但还不能自己回头想明白。**
