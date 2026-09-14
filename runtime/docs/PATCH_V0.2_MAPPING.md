# PATCH v0.2 章节 → 代码映射表

把 `PATCH_v0.2_即时演出与持久认知分离_移除本地2B核心依赖.md` 的每一节（§0–§33）映射到本仓库中的**代码位置 / 测试文件 / 实现状态**。

**本文只记录事实。未实现的条目一律显式标注，不做"看起来差不多"的推测。**

| 项目 | 值 |
|---|---|
| 补丁文件 | `F:\理解痞老板\PATCH_v0.2_即时演出与持久认知分离_移除本地2B核心依赖.md`（1352 行，§0–§33） |
| 代码根 | `runtime/src/companion_runtime/` |
| 测试根 | `runtime/tests/` |
| 最近一次提交 | `15b39d7`（`feat(runtime): split acting layer from persistent cognition per PATCH v0.2`） |
| 核对时的测试规模 | `python -m pytest -q` 收集 **608 项，全部通过**（其中 v0.2 相关：`test_semantic.py` 53、`test_providers.py` 73、`test_deep_refresh.py` 约 40、`test_acting_layer_independence.py` 11、`test_cognition_api.py` 9）。数量随开发持续变化，以实际运行为准 |
| 核对方式 | 直接阅读源码 + 运行测试 + `git status` / `git diff` 比对 |

> **关于工作区状态的提醒。** 核对时，深层刷新那一批改动（`deep_refresh.py`、`runtime.py` 的 `deep_refresh()`、`reducer.py` 的 `_apply_deep_refresh`、`api.py` 的 `/cognition/*`、`cli.py` 的 `refresh`/`backlog`、`typing.py` 的 `TaskKind.DEEP_REFRESH` 等）**尚未提交**，`git status` 显示为 `M` / `??`。这可随时用 `git status --porcelain` 复核。本文描述的是**工作区当前状态**。

## 状态图例

| 状态 | 含义 |
|---|---|
| **已实现** | 代码在位，有测试，且**存在生产调用方**（真的会在运行时生效） |
| **部分实现** | 代码在位但链路不完整，或只覆盖了补丁要求的一部分（每条都注明差在哪） |
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
| §4 | 即时演出层（由主 LLM 负责） | 宿主侧主 LLM；Runtime 侧只负责不做（`runtime.py::MessageOutcome` 记录本轮结算结论） | `test_acting_layer_independence.py`、`test_api.py` | **已实现** |
| §4.1 | 主 LLM 本来就拥有即时理解能力 | `context.py::PRIORITY_PREAMBLE`、`context.py::SECTION_PSYCH`（"进入本轮前的长期状态（背景）"）、`context.py` 渲染块末尾的"它描述的是你进入本轮之前的长期状态，不是本轮该怎么反应" | `test_context_block_is_marked_as_background` | **已实现** |
| §4.2 | 即时演出 ≠ 持久状态写入 | 不变量 6；`runtime.py` 中 `appraisal_source` 的 `coarse_rule / deferred` 语义 | `test_invariant_6_main_llm_has_no_state_write_authority` | **已实现** |
| §5 | 持久认知层 | `emotion.py`、`memory.py`、`unfinished.py`、`user_model.py`、`candidate.py`、`motivation.py` | 各模块对应测试文件 | **已实现** |
| §5.1 | Runtime 目标从"即时理解"改为"长期连续性" | `semantic.py::potential_relevance`、`projections.py::SemanticProjection` | `test_semantic.py::TestRelevancePrioritisation` | **已实现** |
| §6 | 新的完整当前轮流程 | `runtime.py::Runtime.process_user_message`（`lazy_tick` → 事件落库 → 边界 → 工作局势 → 粗粒度结算） | `test_acting_layer_independence.py` | **已实现** |
| §7 | 当前消息与旧 Runtime 心理状态的优先级 | `context.py::PRIORITY_PREAMBLE` | `test_delivery_scheduler_context.py`、`test_acting_layer_independence.py` | **部分实现** — preamble 只写了 5 级：`宿主设定与安全约束 > 当前用户原话 > 当前确定事实 > 显式边界 > 长期状态`；补丁的第 5–6 级（Runtime 持久心理状态、心理解释缓存）与第 7 级（主 LLM 自然发挥）被合并成一句"这里的长期状态"，未逐级进入 prompt 文本 |
| §7.1 | 优先级示例（旧缓存说"失落"，新消息说"回来陪你"） | 无专门代码：这是主 LLM 的行为约定，Runtime 只提供背景 | 无专门测试 | **部分实现** — 机制在（preamble 明确"如果当前用户原话与下面的长期状态不一致，以当前用户原话为准"），但"旧缓存不得压制当前原话"没有可断言的代码路径，只能靠 prompt 约定 |
| §8 | "即时反应"与"情绪余波"正式分离 | `runtime.py`（结算 → `apply_new_emotion_events`；未结算则不产生余波） | `test_ambiguous_events_are_deferred_not_guessed` | **已实现** |
| §9 | 移除"每轮精确情绪评价"的要求 | `semantic.py::classify_event` 的 `None` 路径 + `MessageOutcome.appraisal_source == "deferred"` | `test_semantic.py::TestAmbiguousEventsAreNotSettled` | **已实现** |
| §10 | 粗粒度持久情绪更新（八类方向，不命名情绪） | `semantic.py::ANCHORS`、`_BAND_INTENSITY`、`settlement_to_evaluation` | `test_semantic.py::TestExplicitAnchorsSettle`、`TestAnchorTableIntegrity` | **已实现** — 八类方向都有锚点覆盖：明确正向（`explicit_positive_feedback` / `explicit_joy`）、明确负向（`explicit_distress`）、明确拒绝（`explicit_refusal`）、明确感谢（`explicit_positive_feedback`）、明确冲突（`explicit_conflict`）、明确和解（`explicit_repair`）、明确边界（`explicit_need_for_space`）、明显重大事件（`major_loss` / `major_setback`） |
| §10.1 | 示例 `{direction, impact, source}` | `semantic.py::CoarseSettlement.to_dict` | `test_anchor_settles_with_the_expected_reading` | **已实现（字段名不同）** — 实现输出 `direction / intensity / confidence / source / evidence / semantic_label`（强度用 band 名，而不是补丁示例里的键名 `impact`），语义等价；`semantic_label` 恒为 `None` |
| §11 | 模糊事件允许保持未解释 | `semantic.py::classify_event` 的歧义否决 + `projections.py::SemanticProjection.record_unresolved` + `semantic.py::UnresolvedRecord` | `test_exact_patch_example_is_unresolved`、`test_every_ambiguity_marker_is_covered_by_a_negative_case`、`test_raw_event_survives_being_unresolved` | **已实现** — 「算了，也没什么。」被点名为必须 unresolved，且有直接断言 |
| §12 | 延迟理解从兜底升级为正式特性 | `semantic.py::SemanticStatus.UNRESOLVED`、`event_semantics.unresolved_reason`、`semantic.py::resolve_backlog`、`Runtime.deep_refresh`（低频回头结算） | `test_unresolved_events_accumulate_for_a_later_refresh`、`test_deep_refresh.py::test_a_grounded_reinterpretation_settles_the_backlog` | **已实现** — "记下来"与"之后回头结算"两条都接通了（见 §18–§21）。附注：`resolve_backlog()` 本身仍无生产调用方，刷新路径直接读 `list_unresolved()` |
| §13 | "追夫火葬场"：允许错过当下，但不能丢失原始证据 | `eventlog.py`（append-only，不变量 1）、`event_semantics` 表、`reducer.py::Reducer._apply_reinterpretation`（新版本 + `reappraisals`，从不回写旧事件） | `test_invariant_1_raw_events_are_never_modified`、`test_invariant_7_reinterpretation_never_rewrites_the_past`、`test_deep_refresh.py::test_history_is_not_rewritten_by_a_reinterpretation` | **已实现** — 证据保全 + 追加式重解释 + 低频自动触发齐备。附注：触发需要宿主/运维发起一次刷新（`POST /cognition/refresh`），Runtime 自身没有定时器 |
| §14 | 心理解释不再是高频必需品 | `emotion.py::EmotionExplainer._render`（provider 优先，拿不到就 `_render_template`） | `test_emotion_boundaries_unfinished.py`、`test_providers.py::test_explain_state_success_and_template_fallback` | **已实现** |
| §14.1 | 模板只负责长期底色 | `emotion.py::TEMPLATES_POSITIVE / TEMPLATES_NEGATIVE / TEMPLATES_NEUTRAL`、`EmotionExplainer._render_template`、`context.py::SECTION_PSYCH` | `test_delivery_scheduler_context.py`、`test_context_block_is_marked_as_background` | **已实现** — 模板措辞全部是"进入本轮之前…的底色"口径；测试断言注入块含"背景"与"当前" |
| §15 | 深层心理解释变成缓存 | `db.py::emotion_explanations`、`projections.py`（`store_explanation` / `cached_explanation`）、`EmotionExplainer.explain_and_store`、`reducer.py::Reducer._apply_interpretation_cache`、`Runtime.deep_refresh` 把 `psychological_interpretation` 装进同一条 proposal | `test_deep_refresh.py::test_the_interpretation_cache_is_updated_and_reused`、`test_providers.py`（`state_key` 缓存契约） | **部分实现** — 缓存、复用、`source="deep_refresh"` 写入与"下一轮直接复用"都在位；补丁要求的 stale 判据（"背景心境明显变化 / 主要活跃事件变化 / 重大重估事件出现"）目前只有 TTL 近似：`task.explain_cache_ttl_seconds = 1800`；`semantic.interpretation_max_age_seconds = 21600` **未被任何生产代码读取** |
| §16 | 本地 2B 正式从标准架构移除 | `local_llm.py` 保留，但**只被 `providers.py` 引用**；`Runtime` 不 import 它；`config.SemanticConfig.provider` 默认 `"disabled"` | `test_ingest_works_with_no_provider_configured`、`test_no_outbound_socket_is_opened_during_ingest` | **已实现** |
| §17 | 可选 Semantic Provider | `providers.py`：`SemanticProvider` / `DisabledProvider` / `LocalCPUProvider` / `LocalGPUProvider` / `RemoteAPIProvider` / `build_provider` / `resolve_provider_name` | `test_providers.py`（73 项：四种实现的选择与回落、fail-open、密钥卫生） | **已实现** |
| §18 | 强语义模型的新职责：深层认知刷新 | `providers.py`（端口与契约）、`deep_refresh.py`（`evaluate_triggers` / `build_request` / `ground_suggestions`）、`runtime.py::Runtime.deep_refresh`（编排）、`reducer.py::Reducer._apply_deep_refresh`（落地） | `test_deep_refresh.py`（约 40 项） | **已实现** — 管线为：开关 → provider 可用性 → 触发判定 → 只读组装 → 调用 → grounding → 一条 Proposal → Reducer。任一阶段都可以拒绝，且拒绝原因全部如实上报 |
| §19 | 深层认知刷新输入 | `providers.py::DeepRefreshRequest`（9 个字段与补丁清单一致）、`deep_refresh.py::build_request`（真正组装它：unresolved + 工作局势 + 心境 + 活跃情绪 + 记忆 + 未尽之事 + 用户模型摘要 + 候选 + 关键原文） | `test_deep_refresh.py::TestRequestAssembly`（含"组装请求不写库"与"key_quotes 来自真实事件"） | **已实现** |
| §20 | 输出建议集（六个字段，只有建议权） | `providers.py::DEEP_REFRESH_FIELDS` / `parse_deep_refresh` / `DeepRefreshSuggestions`；`deep_refresh.py::FIELD_TO_KIND` 把六个字段映射成六种操作；`reducer.py::Reducer._apply_deep_refresh` 逐条落地 | `test_providers.py`（逐字段类型校验、部分畸形、`suggestions` 包装键）、`test_deep_refresh.py::TestGrounding` | **已实现** — 六种 `kind` 都能到达落地端（`test_every_operation_kind_is_reachable`） |
| §21 | 深层认知刷新何时触发 | `deep_refresh.py::evaluate_triggers` + `TRIGGER_PRIORITY`（八条规则，优先级顺序，第一条命中者胜出；最小间隔为前置否决）；六类信号可经 `POST /cognition/refresh` 的 `trigger_context` 传入 | `test_deep_refresh.py::TestTriggerPriority`（含"优先级表与补丁顺序一致"、"最小间隔压过所有理由"） | **部分实现** — 触发**判定**完全实现；但**没有内置调度器**：`scheduler.py` 不引用深层刷新，没有任何代码会自动调用 `Runtime.deep_refresh()`，必须由宿主或运维发起（HTTP / CLI） |
| §22 | 新架构三级结构 | Level 0：`runtime.py` / `motivation.py` / `boundaries.py` / `unfinished.py` / `protocol.py`；Level 1：`semantic.py` / `emotion.py` / `memory.py` / `user_model.py`；Level 2：`providers.py` + `deep_refresh.py` | 各模块测试 | **已实现** |
| §23 | 新总流程 | `runtime.py::Runtime.process_user_message`（前半段）→ `Runtime.deep_refresh`（"必要时低频深层认知刷新"）→ `reducer` 落地 → 影响未来轮次 | `test_acting_layer_independence.py`、`test_deep_refresh.py::TestRefreshOrchestration` | **已实现** |
| §24 | 主 LLM 与 Runtime 的新权力边界 | `reducer.py`（唯一写者）、`protocol.py`（APPLY/REBASE/DISCARD）、不变量 3 与 6 | `test_invariant_3_background_models_never_write_directly`、`test_invariant_6_main_llm_has_no_state_write_authority` | **已实现** |
| §25 | 最重要的新设计原则 | 文档 + 代码注释（`semantic.py` 模块 docstring、`deep_refresh.py` 模块 docstring、`providers.py` 三条契约） | — | **文档/约定** |
| §26 | 对"情绪模块"的重新定义 | `emotion.py`（数值动力学 + 长期底色）+ `semantic.py`（粗粒度方向/强度/时间/来源） | `test_semantic.py`、`test_emotion_boundaries_unfinished.py` | **已实现** |
| §27 | 对"情绪解释器"的重新定义（低频语义压缩器） | `emotion.py::EmotionExplainer`（模板兜底）+ `providers.py::explain_state` + `reducer.py::Reducer._apply_interpretation_cache` + `context.py::_optional_explanation_provider`（接线点） | `test_providers.py`（含 `state_key` 缓存契约）、`test_delivery_scheduler_context.py`、`test_api.py` | **已实现** — 解释器、模板、缓存写入、深层刷新填充与 provider 接线都在位：`context.runtime_explanation()` 与 `POST /explain` 都会把（可用的）provider 交给 `EmotionExplainer`，不可用时安静退回模板。附注：stale 判据仍用 TTL 近似，见 §15 |
| §28 | 关键路径预算的最终理解 | `runtime.py::Runtime.process_user_message` 不含任何模型调用；刷新只在独立的 `deep_refresh()` 路径上 | `test_no_outbound_socket_is_opened_during_ingest`（直接拦 `socket.connect`）、`test_deep_refresh.py::test_ingest_does_not_trigger_a_refresh` | **已实现** |
| §29 | 对弱 VPS 的最终意义 | 无代码；落点在 `README.md` 第 15 节（含实测 CPU 成本量级） | — | **文档/约定** |
| §30 | 旧流程与新流程对比 | 无代码；落点在 `README.md` 第 0 节与第 9.2 节 | — | **文档/约定** |
| §31 | 典型例子：用户说"算了，也没什么" | `semantic.py::AMBIGUITY_MARKERS`（含 `算了` / `也没什么`）、`classify_event`、`record_unresolved`；后续重解释走 `deep_refresh` → `reducer._apply_reinterpretation` → `reappraisals` | `test_exact_patch_example_is_unresolved`、`test_ambiguous_events_are_deferred_not_guessed`、`test_raw_event_survives_being_unresolved`、`test_unresolved_events_do_not_leak_into_the_block_as_facts`、`test_a_grounded_reinterpretation_settles_the_backlog` | **已实现** — 前半段（不瞎猜、不丢证据）与后半段（后来重新解释并生成 `reappraisal`）都已接通；区别只在于触发那一步需要一次 `POST /cognition/refresh`（无内置定时器） |
| §32 | 最终架构哲学修正 | `semantic.py` / `providers.py` / `deep_refresh.py` 的模块 docstring、`README.md` 第 0 节与第 5 节 | — | **文档/约定** |
| §33 | 最终结论 | `providers.py::build_provider`（默认回落 `DisabledProvider`）、`config.py::SemanticConfig.provider = "disabled"`、`local_llm.py` 不在任何生产调用链上 | `test_providers.py`、`test_acting_layer_independence.py` | **已实现** |

---

## 二、必须诚实标注为「未实现」的条目

以下每一条都经过源码核对：**没有任何生产代码路径会执行它**（只有定义、配置、注释或测试）。

> **本节已于修复后复核。** 初版审计列出的 9 条中，第 1、2、3、4、5 条**已经修好并加了回归测试**
> （`tests/test_refresh_scheduling.py`、`tests/test_semantic_config_knobs.py`），
> 下表保留原条目并标注现状，而不是把它们删掉——"曾经声明了却没接线"这件事本身值得留档。

| # | 条目 | 现状 |
|---|---|---|
| 1 | **深层刷新没有内置自动调度** | **已修复。** `Runtime.endogenous_round()` 现在会先跑一次 `deep_refresh`（可经 `deep_refresh=False` 关闭），逐轮上报 `EndogenousOutcome.deep_refresh`；刷新失败只记录、绝不打断主动决策。回归测试：`TestRefreshRunsUnattended` |
| 2 | **`semantic.resolve_backlog()` 无生产调用方** | **已修复（换了更合适的位置）。** `deep_refresh.build_request()` 现在按 `unresolved_max_age_hours` 过滤刷新候选；`resolve_backlog()` 仍无调用方，但它的策略已经生效。测试：`TestUnresolvedMaxAge` |
| 3 | **`SemanticConfig.template_fallback` 无消费者** | **已修复。** `EmotionExplainer._render()` 在关闭兜底且无可用 provider 时返回空，`context.render_block()` 只有在真有正文时才输出该段。测试：`TestTemplateFallback` |
| 4 | **`SemanticConfig.interpretation_max_age_seconds` 无消费者** | **已修复。** `EmotionExplainer._explanation_ttl_seconds()` 优先生效该值，未设时回落 `task.explain_cache_ttl_seconds`。测试：`TestInterpretationMaxAge` |
| 5 | **`explain_state()` 未接线** | **已修复。** `context.runtime_explanation()` 与 `POST /explain` 都经 `_optional_explanation_provider()` 传入 provider；不可用的 provider 不会被传（避免在上下文路径上白跑一次）。测试：`TestExplainerUsesTheProvider` |
| 5b | **六类触发信号依赖调用方提供** | **部分修复。** `candidate_pool_size` / `matter_due` / `hours_since_last_refresh` 现在由 `Runtime._refresh_signals()` 自行计算；`major_event` / `history_suspect` / `user_evidence_overturns` 仍须调用方给出——这三项 Runtime 无法自行判断，因此默认按"不成立"处理，而不是猜成成立 |
| 6 | **优先级未逐级进入 prompt** | **已修复。** `PRIORITY_PREAMBLE` 现在逐条列出补丁 §7 的 7 级链，并写明"第 2 项与第 5、6 项冲突时以第 2 项为准"。测试：`TestPriorityPreambleIsComplete` |
| 7 | **embedding 检索仍是词法降级** | **未实现（有意）。** `memory.py::MemoryStore.retrieve()` 是词面重合 + 结构化加权，接口是留给 embedding sidecar 的接缝。补丁 §29 提到的"可选轻量 embedding"不存在；语义相近但用词不同的记忆检索不到。这是记录在案的降级，不是缺陷 |
| 8 | **无常驻巩固 worker** | **未实现（既有边界，与 v0.2 无关）。** 巩固由调用方驱动（`memory.consolidate()`），没有后台线程 |
| 9 | **无 Prometheus 指标导出** | **未实现（既有边界）。** 只有 `/health`、`/maintenance/verify`、`/outbox`、`/cognition/backlog` 的结构化输出，运维需自己抓 HTTP |

---

## 三、部分实现条目：「差在哪」一句话清单

| 补丁章节 | 差在哪 |
|---|---|
| §7 / §7.1 | 已补齐 7 级链；§7.1 的示例行为仍只能靠 prompt 约定，没有可断言的代码路径（Runtime 只提供背景，无法强制主 LLM 怎么做） |
| §10.1 | 输出字段名与补丁 JSON 示例不完全一致（`intensity` band 名 vs `impact`），语义等价 |
| §15 | 缓存、事件驱动失效（cache key 由当前心境与最高强度算出）、TTL 兜底三者齐备；补丁列举的"重大重估事件"单独作为失效信号尚未实现——重估会改变心境与活跃事件，因此通过 cache key 间接失效 |
| §21 | 八条规则 + 最小间隔否决齐全，且已由心跳自动调用；"空候选池"与"空闲"两条额外要求**存在待刷新素材**才会成立，避免全新 Runtime 反复空跑 |
| §12 / §13 / §31 | 主链路已通（含自动重解释与积压结算）；唯一保留条件是"需要有人发起一次刷新"，且 `resolve_backlog` 未被使用 |

---

## 四、v0.2 关键链路的调用图（便于快速定位）

```text
用户消息 ──► POST /events
             └─► Runtime.process_user_message            runtime.py
                 ├─ lazy_tick                              runtime.py
                 ├─ raw_events 落库                        eventlog.py
                 ├─ 边界 / 工作局势 / 未尽之事              boundaries.py, unfinished.py
                 └─ semantic.classify_event                semantic.py
                     ├─ 命中且无否决 → CoarseSettlement
                     │   ├─ projections.semantics.record_settlement   projections.py
                     │   └─ emotion.apply_new_emotion_events          emotion.py
                     └─ 否则 → record_unresolved（本轮无情绪余波）

低频（宿主/运维发起）──► POST /cognition/refresh  或  companion-runtime refresh
                         └─► Runtime.deep_refresh                runtime.py
                             ├─ config.semantic.deep_refresh_enabled
                             ├─ semantic_provider.available()    providers.py
                             ├─ deep_refresh.evaluate_triggers   deep_refresh.py
                             ├─ deep_refresh.build_request       deep_refresh.py（只读）
                             ├─ provider.deep_refresh(request)   providers.py
                             ├─ deep_refresh.ground_suggestions  deep_refresh.py
                             └─ Reducer.process_proposal         reducer.py
                                 └─ _apply_deep_refresh
                                     ├─ reinterpretation → interpretation_versions + reappraisals
                                     ├─ psychological_interpretation → emotion_explanations
                                     ├─ candidate_intent → pool.py
                                     ├─ memory / unfinished_matter / user_model_evidence
                                     └─ applied 非空 → semantics.settle_from_deep_refresh
```

---

## 五、怎么自己复核这些结论

```powershell
cd F:\理解痞老板\runtime

# 1) 测试规模与状态（数字随开发变化，以你的运行为准）
python -m pytest -q

# 2) 深层刷新的调用点有几个？（预期：api.py + cli.py 两处，scheduler.py 零命中）
Get-ChildItem -Recurse src -Filter *.py | Select-String "deep_refresh\("

# 3) 调度器有没有自动触发？（预期：无输出）
Select-String -Path src\companion_runtime\scheduler.py -Pattern "refresh"

# 4) 哪些 SemanticConfig 旋钮还没被消费？（预期：这三项只在 config.py 命中）
Get-ChildItem -Recurse src -Filter *.py |
    Select-String "template_fallback|interpretation_max_age_seconds|unresolved_max_age_hours"

# 5) resolve_backlog 有没有生产调用方？（预期：只有 semantic.py 的定义 + test_semantic.py）
Get-ChildItem -Recurse src,tests -Filter *.py | Select-String "resolve_backlog"

# 6) explain_state 有没有被接上？（预期：两处都带 provider=_optional_explanation_provider(runtime)）
Select-String -Path src\companion_runtime\context.py,src\companion_runtime\api.py -Pattern "EmotionExplainer\(" -Context 0,4

# 7) 补丁点名的那句话是否真的保持 unresolved？（预期：None）
$env:PYTHONPATH="src"; python -c "from companion_runtime.semantic import classify_event; print(classify_event('算了，也没什么。'))"

# 8) deep_refresh 的敏感度条目在不在？（预期：命中，值为 "low"）
Select-String -Path src\companion_runtime\protocol.py -Pattern "DEEP_REFRESH"

# 9) 工作区里哪些改动还没提交
git status --porcelain
```

---

## 六、一句话总结

补丁 v0.2 的**架构性结论已经落地并可验证**：Runtime 以零模型完整运行、入口路径不做任何模型调用（有拦 `socket.connect` 的结构性测试）、显式事件粗粒度结算、模糊事件诚实记为 `unresolved` 且永不丢失原始事件、心理解释退化为模板 + 缓存、本地 2B 降级为可选的 `LocalCPUProvider`。

**"后来想明白"这条链也已经接通**：触发判定（八条规则 + 最小间隔否决）→ 只读组装 → provider → grounding（引用不到真实实体就丢弃并记账）→ 一条 Proposal → Reducer 的 APPLY/REBASE/DISCARD → **只有被成功应用的操作真正引用过的事件**才离开 unresolved（`test_only_referenced_events_leave_the_backlog`），重解释永不回写历史。

**剩下的都是"接线"而不是"缺件"**：没有内置调度器去自动发起刷新（要宿主或运维调用 `POST /cognition/refresh` / `companion-runtime refresh`）、`resolve_backlog()` 与 `unresolved_max_age_hours` 未被使用、`template_fallback` 与 `interpretation_max_age_seconds` 尚未被消费、优先级 preamble 只声明到 5 级。逐条见第二节。
