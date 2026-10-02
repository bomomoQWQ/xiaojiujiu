# 当前 HEAD 生产静态调用链审计

- 审计对象：`32bd8a2fbad167c3f6c66d99e704268208d5fed5`
- 方法：只读静态设计—实现审计；不运行服务、不改数据库、不提交。
- 范围：生产入口 `cli/composition` → authority router → Runtime-v2 / 浪潮 shadow / 浪潮 live；Goal / Reward / ActionCandidate / Outcome；social sources；render / send / ACK；训练 labels。
- 机器可读配套：[`static_consumers.json`](./static_consumers.json)

## 1. 证据等级与总判定

证据等级（下文“证据 C”）：

- **C3（生产可达）**：从 `cmd_serve` 的生产 composition 有连续静态调用链，且写表/权限边界可定位。
- **C2（实现存在但生产未接线或仅条件可达）**：实现与测试可见，但 production composition 没有生产者/调用者，或必须显式配置后才有路径。
- **C1（数据契约/占位）**：DTO、字段、表或注释存在，但缺少闭环消费者。
- **C0（缺失）**：目标能力没有实现路径。

### 结论摘要

1. **生产总入口与 authority-first 路由是 C3。** `cmd_serve` 强制关闭 legacy user model/endogenous，构造 PostgreSQL-only v2 graph，并由 scheduler 调 `authority_round_router.run`（`cli.py:298-365`）。router 在任何 engine 执行前读 active authority（`langchao_live_wiring.py:114-146`）。
2. **Runtime-v2/live 是唯一默认可发送路径。** composition 在 authority 缺失时 bootstrap，默认 `runtime_v2/live`；v2 决策执行 hazard、audit、dispatch claim、legacy attempt/render outbox 与 committed snapshot（`composition_v2.py:177-246`; `runtime_v2.py:510-774`; `legacy_bridge_v2.py:175-237`）。
3. **langchao/shadow 不发送、不训练、不计 quota/reward，但会写合同、预期 outcome、状态与 shadow audit。** capability wrapper 仅暴露 authority read（`langchao_shadow_wiring.py:68-75`）；shadow record 强制四类 side-effect count 为 0 且 outbox null（`langchao_shadow.py:165-227`）。共享 evaluator 即使服务于 langchao/live，仍把 round/audit 持久化为 `mode/run_mode="shadow"`，见缺口 G11。
4. **langchao/live 先让 v2 做 assessment-only，再做浪潮评估；只有 allowlist + live runner + active `langchao/live` 才可 commit。** v2 comparator 不跑 exploration counter/RNG/audit/commit（`runtime_v2.py:590-609`）；live 重验 scope、active revisions、state version、permission、capability、sources、当前 boundary（`langchao_live.py:75-200`）。
5. **Goal/Reward/Candidate/Expected Outcome 是生产可达的浪潮 shadow/live 合同账（C3，配置条件）。** 它们由 runtime-v2 candidate/prediction 快照确定性构造，按 scope 写 immutable revisions + CAS active pointers（`langchao_runtime_adapter.py:303-416`; `langchao_shadow_service.py:158-202`; `langchao_repository.py:86-237`）。
6. **Outcome 实际结算只覆盖本地可见执行结果。** langchao live ACK 只结算 `delivery` / `expression_delivered` / `rest_realized`；reply/continuation/negative 保持未观测，失败写 censored、金额 0（`langchao_live_repository.py:86-138`）。目前没有从 v2 target labels 回灌浪潮 outcome ledger 的代码（C0 缉获）。
7. **social 子系统为 C2：实现完整但 production composition 未实例化。** `build_social_proposal` 与 `LangchaoSocialRepository` 只在模块/测试中被引用；生产 composition 不创建它。更关键的是当前 legacy `CandidateV2.action` 不含 `memory_ref/social_ref/unfinished_id`，而浪潮 adapter 对 expression/followup 要求这些显式 refs，因此这些候选会被丢弃，而不是使用 social source（`legacy_bridge_v2.py:384-395`; `langchao_shadow_wiring.py:139-184`）。
8. **关键专项判定：实际发送后的训练观测没有学习最终文本。** render 文本进入 legacy `action_attempts.rendered_text` 并用于发送前 authorize，但 ACK → v2 exposure 使用的是 commit 时冻结的 `chosen.action`（type/intent/goal/target + 布尔形状），不是 `rendered_text`；feature encoder 也只读这些布尔动作特征和上下文。最终文本既不进 `interaction_exposures_v2.action`，也不进 `feature_snapshot`，因此训练不会区分同一 candidate/action 下的不同最终措辞（C3 反证链）。
9. **旧 v1 学习/收益链虽仍在源码，但 production serve 明确禁用。** 旧 user model/legacy endogenous 的预测、效用、观察与 `interaction_observations/user_model_params/decisions` 写入不可作为当前 HEAD 生产消费者证据；入口禁用见 `cli.py:316-320`。

## 2. 生产入口与 authority 路由

### 2.1 CLI → composition → scheduler

```text
companion-runtime serve
  cli.cmd_serve
    Runtime(config)                         # legacy 仅保留机械 foreground/delivery
    ConcreteLegacyRuntimeV2Bridge(runtime)
    build_v2_composition(scope=config.conversation_id)
      require_postgres_dsn + migrate
      authority.get_active || bootstrap()
      UserModelV2Service + PredictionService
      PostgresV2RuntimeRepository
      V2RuntimeCoordinator
      [opt-in] LangchaoShadowRunner
      [allowlist] LangchaoLiveRunner
      AuthorityRoutedEndogenousRound
    runtime.v2_coordinator = coordinator
    runtime.langchao_live_runner = live_runner
    Scheduler(callback=run_v2_round)
      authority_round_router.run(...)
      V2Maintenance.run_due(fit=True)
      legacy rule-based memory consolidation
```

证据：`cli.py:298-380`；`composition_v2.py:131-285`。`config.legacy_user_model_enabled=False` 与 `legacy_endogenous_enabled=False`（`cli.py:316-320`）使生产学习/主动决策归 v2，而不是旧 user model/motivation。

### 2.2 authority truth table

| Active authority | router 执行 | hazard/RNG | decision audit | commit/send | 浪潮合同/状态/audit |
|---|---|---:|---:|---:|---:|
| `runtime_v2/live`, may_dispatch=true | `v2.decide_endogenous`; 可附带浪潮 shadow | 是 | v2 是 | v2 可 | shadow 启用时是 |
| `langchao/live`, may_dispatch=true | `v2.assess_endogenous` → 浪潮 live | v2 否；浪潮用确定性积分 | v2 否；浪潮是 | 仅 allowlist/live runner 后浪潮可 | 是 |
| `langchao/shadow` | `v2.assess_endogenous` | 否 | v2 否 | 否 | 当前 router **不会显式调用** `langchao_shadow_runner`；见缺口 G2 |
| `none/disabled` 或其他 | `v2.assess_endogenous` | 否 | 否 | 否 | 否 |

直接依据：`langchao_live_wiring.py:114-146`。权限记录与 claim 是 scope-bound immutable revision/CAS：`langchao_authority_repository.py:86-208,252-400`；发送能力判断 `authority_may_dispatch` 在 `langchao_authority.py:28-31`。

> **G2：独立 `langchao/shadow` authority 下 router 只返回 v2 assessment，没有调用 shadow runner。** shadow runner 只在 `runtime_v2/live` 分支作为附加 comparator 被调用（`langchao_live_wiring.py:118-127`），以及 `langchao/live` 的 live evaluator 内被调用。故“切换到 langchao/shadow 后持续生成 shadow runs”不由当前 production router 代码成立。

## 3. 三条运行模式调用链

### 3.1 Runtime-v2/live（C3）

```text
router
 → V2RuntimeCoordinator.decide_endogenous
   → legacy.candidates / boundary_verdict
   → repository.prediction_for
   → user_utility(reply lower, continue lower, negative upper)
   → repeat cost
   → net = internal_utility + user_utility - repeat_cost
   → threshold / cold-start gate / hazard RNG
   → ConcreteLegacyRuntimeV2Bridge.commit_candidate_with_snapshot
     → create_live_dispatch_claim(expected_engine=runtime_v2)
     → Runtime._commit_attempt → action_attempts + render outbox
     → runtime_v2_committed_decisions
 → render result → complete_render(rendered_text) → send outbox
 → pre-send authorize(final rendered text)
 → send result → mark_delivered
 → coordinator.after_legacy_send_ack
   → interaction exposure + pending labels + frozen expectation
```

证据：`runtime_v2.py:525-649,721-774,799-921`；`legacy_bridge_v2.py:175-237`；`api_v1.py:1397-1499,1817-1867`。

### 3.2 浪潮 shadow（C3 when attached to runtime_v2/live；C2 as standalone authority）

Runtime-v2 的 assessments 被映射成固定模板合同：contact / expression / followup / internal_rest；unsupported repair/apology/confront 被显式丢弃（`langchao_shadow_wiring.py:139-184`）。adapter 生成 Goal→Reward→bound Goal→Candidate 和 expected Outcome tokens（`langchao_runtime_adapter.py:303-416`）。shadow service 在单事务中写合同、outcome、state、integration steps 与 shadow audit；没有 sender/outbox/exposure/quota capability（`langchao_shadow_service.py:245-300`; `langchao_shadow.py:231-244,533-548`）。

### 3.3 浪潮 live（C3，但需配置+allowlist+authority）

```text
router reads langchao/live
 → v2.assess_endogenous(commit=False)
 → LangchaoLiveRunner.run
   → shadow evaluator builds/persists contracts + numerical audit
   → before_commit callback in same outer transaction
     → LangchaoLiveService.execute_in_transaction
       → live revalidation
       → two claims (mechanical live claim + semantic candidate claim)
       → same legacy _commit_attempt/render outbox
       → langchao_live_commits snapshot
 → legacy render/send
 → api_v1 routes ACK by persisted outbox engine marker
 → LangchaoLiveRepository.settle_terminal
```

证据：`langchao_live_wiring.py:41-75,82-140`; `langchao_shadow_service.py:295-300`; `langchao_live.py:75-153`; `legacy_bridge_v2.py:239-329`; `api_v1.py:1481-1499`。

## 4. Goal / Reward / Candidate / Outcome 生产—消费矩阵

| 对象/指标 | 生产者 | 消费者 | mode | scope / 权限 | 写表 | 证据 C | 缺口 |
|---|---|---|---|---|---|---|---|
| GoalContract | `langchao_runtime_adapter._build_one` | shadow persist；live `_validate_contract` | shadow/live | `scope_key`；builder 无写权，repository 写 | `langchao_goal_identities/revisions/active` | C3 | 只覆盖 4 个模板；Goal 本身不从 social/adopted-goal 管线生成 |
| RewardContract | `_build_one` 固定模板 amount + expected tokens | `compile_candidate_reward`; live revision recheck | shadow/live | scoped immutable + CAS | `langchao_reward_*`, `langchao_reward_outcomes` | C3 | amount 为模板常量；无在线学习/目的注册消费器 |
| ActionCandidateContract | `_build_one` 从 v2 candidate facts | shadow engine；live execute | shadow/live | external 必须 permission/capability/source；live 再查 | `langchao_candidate_*`, goal refs | C3 | unsupported candidate kinds 被丢；legacy action 缺 explicit refs 导致 expression/followup 常被丢 |
| Expected OutcomeToken | `_build_one`：reply/continuation/negative/delivery/local | reward compiler；outcome repository；live snapshot | shadow/live | scoped、immutable、reward exact FK | `langchao_outcome_*`, `langchao_reward_outcomes` | C3 | delivery expected value 固定 0；用户结果无 actual producer |
| Actual local OutcomeToken | live ACK `settle_terminal` | outcome ledger/active pointer；**无后续 reward/state consumer** | live only | terminal ACK exactly-once；只认本地执行 | `langchao_outcome_revisions/active`, `langchao_live_commits` | C3/C1 | 写了账但不会改变 future attraction/readiness；internal_rest 无 send ACK，故 rest actual 路径不可达 |
| v2 reply/continue/negative prediction | active parameter head / prior | `motivation_v2.user_utility`; 浪潮 forecast adapter | v2 + shadow/live assessment | same scope | `prediction_snapshots_v2`（预测服务路径） | C3 | acceptance 有 label/head，但决策明确拒绝消费 |
| v2 internal_utility | legacy candidate `internal_need + max(unfinished,emotion)` | v2 net utility；浪潮仅 provenance/audit，不进浪潮 reward | v2 | candidate-scoped，来自 legacy projections | candidate/decision audit snapshot | C3 | 代理指标，不是 observed reward；与浪潮 attraction 分叉 |
| v2 net_utility | coordinator | threshold、winner、hazard advantage；shadow baseline audit | v2/live | decision scope | `runtime_v2_decision_audits` / committed snapshot | C3 | 不含最终文案效果；非训练 target |
| 浪潮 total_utility / attraction | `compile_candidate_reward` | `advance_langchao` readiness crossing | shadow/live | contract state scope | shadow audit + state candidate/integration tables | C3 | expected forecast 为 v2 预测和模板 prior；actual outcomes 不回灌 future compile |
| readiness | `advance_langchao` 积分 | winner/defer；下一 state | shadow/live | scoped CAS state pointer | `langchao_state_*`, `langchao_integration_steps` | C3 | live internal_rest 不创建 send/ACK，且当前没有独立“rest realized”结算调用 |
| hazard probability/random draw | v2 coordinator | v2 commit gate | runtime_v2/live only | authority-gated scheduler | `runtime_v2_decision_audits` | C3 | langchao/live 的 v2 comparator 不产生它，正确隔离 |
| repeat cost/quota proxy | `evaluate_repeat_v2` | v2 net；浪潮 CandidateCostTerm | v2/shadow/live assessment | scope + acknowledged exposure history | metadata/audit；非单独 quota 表 | C3 | shadow count 字段固定 0；live exposure metadata 只在 v2 exposure ACK 路径，langchao ACK 不创建 v2 exposure |

## 5. social sources 审计

### 5.1 已实现能力（C2）

- `langchao_social.py` 纯规则只接收 `UNFINISHED / ACTUAL_EVENT / ACTIVE_BOUNDARY / SUPERSEDED_MEMORY`，文本只当数据，不可授予 goal/permission/write（`langchao_social.py:1-6,29-70,115-237`）。
- repository 在 commit 时重新解析每个 exact source；scope/kind/id/revision/hash 不一致要求 rebase/discard，写入 append-only items/links/state events，并 CAS projection heads（`langchao_social_repository.py:1-7,127-143,285-300` 及后续 commit 实现）。
- 对应表：`langchao_social_sources/build_runs/proposals/items/links/state_events/projection_state/projection_heads/item_sources/link_sources`（`langchao_social_schema.py`）。

### 5.2 生产断链（G3）

- `composition_v2.py` 不 import/构造 social builder、source resolver 或 social repository。
- 全仓生产源码中 `build_social_proposal` 无调用；`LangchaoSocialRepository` 仅测试使用。
- 浪潮仅在 expression action 已携带 `social_ref` 时接受它（`langchao_shadow_wiring.py:163-171`），但 `ConcreteLegacyRuntimeV2Bridge._candidate` 生成的 action 不含 `social_ref`、`memory_ref`、`unfinished_id`（`legacy_bridge_v2.py:384-395`）。followup 尚可从 `repeat_subject.concern_id` fallback；expression 无 fallback，会被丢。

因此 social 当前是**隔离实现/测试资产，不是生产候选证据生产者或消费者**。

## 6. render → send → ACK → labels

### 6.1 实际调用链

1. commit 将语义 `candidate.action` 放入 render outbox payload（`legacy_bridge_v2.py:222-228` / 浪潮 `305-320`）。
2. adapter 上报 render `result.text`；reducer `complete_render` 把它写入 attempt 的 `rendered_text` 并创建 send outbox（`api_v1.py:1397-1410`; `action.py:266`; `reducer.py:1596+`）。
3. send 前 `/authorize` 读取 attempt `rendered_text`，可校验 SHA-256，并以最终文本执行边界检查（`api_v1.py:1831-1857`）。
4. send ACK 后 `mark_delivered`；按 outbox 持久化 engine marker 路由到 v2 coordinator 或 langchao live runner（`api_v1.py:1460-1499`）。
5. v2 成功 ACK 创建 exposure；失败 ACK 不创建 exposure。langchao 成功/失败 ACK 只结算 local outcome tokens（分别 confirmed 或 censored=0）。

### 6.2 “是否学最终文本”的明确答案：**否**

静态反证链：

- `SendAckV2` 虽有 `action`，没有 text/rendered_text 字段（`runtime_v2.py:157-169`）。
- API 构造 ACK 时传 `_v2_action(runtime,row)`，未传 attempt.rendered_text（`api_v1.py:1488-1497`）。
- coordinator ACK 路径忽略 `ack.action`，而取 committed `chosen.action` 写 exposure（`runtime_v2.py:846-880`）。
- committed `chosen.action` 在 render 之前已冻结；由 bridge 生成，仅有 `type/intent/goal/target` 与 5 个动作布尔（`legacy_bridge_v2.py:384-395`）。
- `FeatureSnapshotV2` 用 exposure 的 `action_json`，encoder 只读 `proactive/follow_up/emotional_expression/question/topic_shift`，没有文本 embedding/hash/style/length（`user_model_v2_service.py:160-168`; `user_model_v2_features.py:217-258`）。
- 训练查询从 `interaction_exposures_v2.feature_snapshot` 联接 active labels（`user_model_v2_service_repository.py:303-342`）；fit 使用冻结 feature vectors 和二元 labels（`user_model_v2_service.py:274-355`）。

**后果：**同一 action shape 下，两条最终文本无论语气、问题内容、长度或安全修订如何不同，训练特征完全相同。模型只能学习“候选类型 + 发送时上下文”的平均响应，不能把回报归因给实际措辞。legacy attempt 表确实保存最终文本，但 v2 training join 不读它。

## 7. training labels 的生产者、消费者与语义

| target | 实际 observation 生产者 | timeout 生产者 | 训练消费 | 状态 |
|---|---|---|---|---|
| reply | 仅 legacy foreground 将用户消息结构归因到一个 sent attempt 后产生 `value=True, explicit=False` | 到窗且 coverage 完整 → false | MAP/Laplace target head；决策用 lower bound | C3 |
| continue | **无事件生产者**（源码仅一个 `TargetObservationV2(...)` 调用，即 reply） | reply observed 后到窗 → false；无 reply → unknown | target head；决策用 conditional lower bound | C2：只能学 false/空，不能学 true |
| negative | **无 explicit observation 生产者** | 到窗无事件 → false | target head；决策用 upper bound | C2：只能学“未见负面”；无法学显式 negative=true |
| acceptance | **无 explicit observation 生产者** | 无事件永远 unknown | 会拟合 head，但 motivation 明确禁止消费 | C1 |

证据：唯一 observation 构造点 `legacy_bridge_v2.py:90-101`；标签规则 `user_model_v2_labels.py:162-199,207-320`；maintenance 空 observations 超时结算 `maintenance_v2.py:159-180`；训练过滤 observed labels `user_model_v2_service.py:274-314`；acceptance 拒绝消费 `motivation_v2.py:187-193`。

## 8. 写表与权限边界摘要

- **legacy mechanical writer**：`raw_events`, projections, `action_attempts`, `attempt_events`, `outbox`; Runtime reducer/bridge 写。主 LLM/render 文本无内部心理状态写权限。
- **v2 model writer**：`interaction_exposures_v2`, `interaction_target_labels_v2`, active labels, parameter snapshots/active pointers, prediction snapshots, expectations, decision audits, committed decisions, exposure metadata, user matter events, expectation settlements。
- **authority writer**：`langchao_authority_revisions/active`, `live_dispatch_claims`, `langchao_dispatch_claims`; claim 必须命中 exact active live authority。
- **浪潮合同/状态 writer**：contract/outcome/state/shadow tables；shadow 没有 sender/outbox/exposure capability。
- **浪潮 live writer**：在 shadow 外层事务的 `before_commit` 中写 claims + legacy attempt/outbox + `langchao_live_commits`；ACK 后写 actual local outcome。
- **social writer**：独立 repository 能写 social tables，但 production composition 未授予/实例化该 capability。

## 9. 缺口清单（按风险排序）

1. **G1 / High / C3：训练不学习最终文本。** 需要在成功 ACK 时从 exact attempt 读取 final rendered text，作为不可变 exposure provenance；若文本不可直接用于模型，至少冻结 `text_sha256`、长度、问句/语气/模板版本或可审计 encoder 版本。必须保证 prediction 与 training 使用同一可获得特征定义，避免 post-treatment leakage。
2. **G2 / High / C3：`langchao/shadow` authority 分支不运行 shadow runner。** router 的 fallback 仅 `v2.assess_endogenous`。应明确这是设计（shadow 只作为 runtime_v2/live 附件）还是漏接线；若后者，在该分支调用 shadow runner，同时保持零 side effect。
3. **G3 / High / C3：浪潮用户 outcome 不闭环。** reply/continuation/negative v2 labels 不会产生浪潮 actual/correction OutcomeToken；actual ledger 仅 local execution，且 future reward compilation 不消费 actual ledger。当前“收益结果账”是审计账，不是学习闭环。
4. **G4 / High / C3：continuation=true、negative=true、acceptance± 没有生产者。** 唯一 observation producer 是 reply=true。negative head 会大量学 timeout false；continuation 缺 true；acceptance 不可用于决策。
5. **G5 / Medium / C3：langchao live 成功 ACK 不创建 v2 exposure/labels。** API 按 persisted outbox engine marker 选择 terminal coordinator（`api_v1.py:1474-1499`），langchao runner 只结算 outcome ledger。因此切为 langchao/live 后，该引擎的实际发送不进入 v2 user-model training/repeat metadata，造成 mode-dependent learning blind spot。
6. **G6 / Medium / C2：social production 未接线。** social tables、builder、authority repository 均存在，但 composition 无实例；候选 adapter 又缺 social refs。
7. **G7 / Medium / C3：local outcome 定价与结算不闭环。** rest 候选 live 返回 `internal_rest` 而不 commit/send（`langchao_live.py:105-108`），actual local outcome 仅 terminal send ACK 产生，所以 `rest_realized` actual token 没有生产入口；而 expression 的 `expression_delivered` 在执行前即可由模板 probability=1 贡献 +0.25 expected attraction（`langchao_runtime_adapter.py:354-369`），但实际账仍无后续消费者。
8. **G8 / Medium / C3：最终 ACK 的 `ack.action` 被忽略。** coordinator 使用 committed `chosen.action` 是防篡改优点，但 API 仍构造 action 字段，形成看似可传执行事实、实际无消费者的死字段；应删除或校验等于 committed action。
9. **G9 / Medium / C3：exposure propensity 固定 1.0。** `put_prepared_exposure` 写 `propensity=1.0`，没有使用实际 hazard probability/selection probability；若未来做反事实/IPW，当前值不是实际行为策略 propensity。
10. **G10 / Low / C2：Goal/Reward 模板范围窄。** repair/apology/confront 明确被 dropped；social/adopted goals 不进入生产 GoalContract。
11. **G11 / Medium / C3：langchao/live 数值阶段被持久化为 shadow mode。** 共享 service 的 `begin_round(..., run_mode="shadow")` 与 `ShadowRunRecord(mode="shadow")` 在 live evaluator 中也不变（`langchao_shadow_service.py:252-281`; `langchao_shadow.py:180-192`），故仅靠 round/audit mode 无法区分真正 shadow 与 live 前置评估。
12. **G12 / Medium / C3：合同部分声明未被 live 强制。** `precondition_refs`、`invalidation_refs`、`resource_budget`、`attempt_budget` 被携带/持久化，但 live validation 展示的检查未消费它们；`permission_ref` 只检查非空，真正授权来自 authority claim。
13. **G13 / Medium / C3：scope identity 失配可令 reply attribution 找不到 exposure。** repository 对非 UUID attempt 以 `scope_key` 做 uuid5（`user_model_v2_service_repository.py:197-212`），bridge 归因则用 `runtime.config.conversation_id`（`legacy_bridge_v2.py:85-99`）；若二者不同，`settleable_exposures` 的 exact ID 查询不命中。
14. **G14 / Low / C3：通用 outbox ACK 不是 send completion。** `POST /outbox/{id}/ack` 只 ack queue，不会 mark attempt sent 或创建 v2 exposure；客户端若误用会丢失 delivery/training linkage。
15. **G15 / Low / C3：scope 参数被 legacy bridge 丢弃。** `candidates` / `boundary_verdict` 丢弃传入 scope，依赖当前单 Runtime 实例（`legacy_bridge_v2.py:127-141`）；当前 one-runtime-per-conversation composition 下成立，复用为多 scope 服务会串域。
16. **G16 / Low / C2：收益分解与 realized token 缺少查询面。** reward compilation 只嵌在 `langchao_shadow_runs.audit` JSON；未见 actual Langchao token 的 observability/maintenance API 消费器。

## 10. 最小验收建议（不在本次只读审计内实现）

- 做一个两条 candidate action 完全相同、最终 render 文本不同的端到端 fixture，证明当前 feature snapshots 相同；修复后要求文本派生特征或 exact text provenance 不同。
- 对四 target 建“生产者覆盖”测试：源码/fixture 中必须能产生 reply=true、continue=true、negative=true、acceptance=true/false，或明确从 contract 删除不可生产 target。
- authority truth-table 测试增加 active `langchao/shadow`，断言有 shadow run 且 send/reward/training/quota/outbox delta 全 0。
- langchao/live external send 全链：commit→render→send→ACK→用户 reply→actual outcome/correction + v2 label（若决定共享训练）→restart recovery。
- social composition smoke：真实 source resolver → build/submit/commit → candidate action 携 `social_ref` → contract input_refs → live revalidation。
