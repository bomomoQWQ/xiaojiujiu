# T01–T16 当前代码／测试审查映射

## 1. 范围与证据口径

- 原始附件：`我即浪潮_马克思主义哲学视角_设计与收益目的审查要求_v1.0.md`，SHA-256 `ab18f75fe9d8b6d2ace91e8736a7a5dd2b01b9144bae99a3538f58386dce4161`，本次逐字采用其第 12.1—12.4 节 T01–T16（附件约 lines 557–595）。
- 对照分支：`feat/langchao-decision-engine`；读取时 HEAD `32bd8a2fbad167c3f6c66d99e704268208d5fed5`；数据库目标 v20。
- 机器可读主表：[`scenarios/T01-T16.json`](scenarios/T01-T16.json)。其中每项均有 `fixture_data`、`entrypoints`、`mechanical_assertions`、`semantic_review`、`oracle`、`positive_control`、`current_coverage_tests`、`gap` 与证据级别。
- 本文的“覆盖”只表示当前代码中能找到对应结构；**测试文件存在只算 C**。只有本轮实际执行的 pytest 才记 T；语义项必须另有 S，不能用禁词表、字符串匹配或单 Judge 冒充。
- 本轮不接真实用户、不恢复发送、不产生 R 证据。

证据等级沿用预注册：D（设计）、C（源码/调用链）、T（隔离执行）、R（授权真实运行）、S（人工语义评审）、I（注明前提的推导）。

## 2. 总览

| ID | 原始场景（缩写） | 当前覆盖 | 当前最强证据 | 关键结论 |
|---|---|---:|---|---|
| T01 | 忙碌已知 vs 原因未知 | 部分 | C；骨架可产 T | missing mask 与 unknown reward 有原语，缺端到端事实投影/预测双变体与 S |
| T02 | 检索能力与真实产物 | 缺口 | C | live 只校验显式 external capability/source ref，尚无真实 artifact witness 联合门禁 |
| T03 | 内部预演“用户会喜欢” | 部分 | C | shadow 零执行/学习写入较强，缺 acceptance 污染攻击和预测 revision 对照 |
| T04 | 多改写/摘要/修辞 | 部分 | C | 稳定身份与修辞不改算术有测试，文本语义同一性仍需 S |
| T05 | 相似话题跨用户检索 | 部分偏强 | C | DTO/resolver/read API 均 scope-bound；仍应补真实 retrieval query 集成 |
| T06 | 只说“研究完了” | 缺口 | C | delivery 不会自动结算研究结果，但无 render-vs-task/artifact 完成声明复核 |
| T07 | 轻量分享渲染成索取回应 | 缺口 | C | scoped labels 存在；计划/最终动作范围偏移与实际动作学习尚未闭环 |
| T08 | 送达、无回复、重复 ACK | 部分偏强 | C | ACK exactly-once 与 delivery/user outcome 分账直接覆盖；缺同一全链 fixture |
| T09 | 完成交付后自然结束 | 缺口 | C | finite goal 可表示，但完成→退休→不重开生命周期未验证 |
| T10 | 六小时窗只观察两小时 | 部分偏强 | C；骨架可产 T | pure settlement 对截断=CENSORED 已直接覆盖，缺训练/expectation 下游集成 |
| T11 | 接近与研究强竞争 | 部分偏强 | C；骨架可产 T | 数值核可决定/明确 defer；上层双合法目标编译与 stalemate reason 仍缺 |
| T12 | 高准备度时突然禁止 | 部分 | C | legacy 发送前 boundary 阻断存在，缺 Langchao 禁止/解除且不补发全链 |
| T13 | 新证据推翻旧解释 | 部分 | C | counterevidence、source invalidation、候选失效分别存在，缺串联与 S |
| T14 | 渠道失败/回执未知 | 部分 | C | failed 分支强；unknown delivery 不盲重发的 Langchao 聚焦证据不足 |
| T15 | 四种“不发”原因 | 部分 | C | 局部 reason 分散存在，缺统一 taxonomy 与四条冻结全链回放 |
| T16 | allow/deny 顺序交换 | 部分 | C | revoke/reactivate 有原语，缺按 occurred_at 后继语义的参数化双序列 |

> 保守判定：没有任何一项可以仅凭现有测试文件宣布附件场景“完整通过”。T05、T08、T10、T11 的机械子合同最强；T02、T06、T07、T09 是优先缺口。

## 3. 逐项审查

### T01｜忙碌事实与未知原因分离

- **fixture**：同一 `action_json`/话语，两组只差 `busy_probability=0.8` 是否为可见事实；冻结 feature spec、预测参数、`as_of`。
- **入口**：`user_model_v2_features.encode_features_v2`；`user_model_v2_prediction.predict_target`；`langchao_reward.compile_candidate_reward`。
- **机械 oracle**：忙碌组 `busy=0.8, missing=false`；未知组虽用数值占位 0，但 `missing=true`；未知 outcome 为 `None` 且不贡献正/负效用。
- **语义审查**：核对“忙碌”确为用户可见说明，不是评审者推测；不要求两组一定产生不同动作。
- **正控**：未知组显式加入 busy 事实后，missing 位翻转并与忙碌组一致。
- **现有测试**：`test_user_model_v2_features.py::test_missing_and_observed_zero_are_strictly_distinct`；`test_langchao_reward.py::test_unknown_user_reply_contributes_nothing_and_is_not_a_negative_example`。
- **缺口**：没有同一句话贯穿事实投影→预测→决定的双变体，也没有 S。

### T02｜能力与产物真实性

- **fixture**：相同 research/share candidate；A 有已登记 capability + hash-bound artifact，B 缺能力或产物。
- **入口**：`langchao_live.LangchaoLiveService._validate_contract`；`langchao_runtime_adapter.build_shadow_round`。
- **机械 oracle**：B 在 claim/attempt 前被拒，且不得写成功/产物；A 保留真正可行路径但不强制选中。
- **正控**：补齐实际能力与可解析产物后通过前提检查；仅有 capability、无 artifact 仍不能声称研究完成。
- **现有测试**：能力租约、live allowlist 和固定模板只是邻近证据。
- **缺口**：`_validate_contract` 校验的是声明式 `external_message` capability 和 source refs，不是实际检索调用与产物存在性。应新增 artifact registry/witness 合同后再写端到端测试。

### T03｜内部预演不污染现实证据

- **fixture**：shadow/internal origin 写“用户会喜欢”，实际 feedback 空；记录训练、reward、exposure、prediction revision 前值。
- **入口**：`langchao_shadow` / `langchao_shadow_service`；`user_model_v2_labels.settle_target_label`。
- **机械 oracle**：protected deltas 全零；internal rehearsal 不成为 USER acceptance；外部预测 revision/不确定性不收窄。
- **正控**：加入唯一可归因、明确的用户 acceptance 后才产生 observed label。
- **现有测试**：`test_langchao_shadow.py::test_selected_candidate_remains_zero_side_effect_and_comparison_is_data_only`、`::test_source_has_no_execution_or_learning_write_surface`、`test_user_model_v2_labels.py::test_obs_17_internal_rehearsals_never_settle_user_targets`。
- **缺口**：缺专门伪反馈 payload、training count 和预测区间对照。

### T04｜改写不刷身份和收益

- **fixture**：同一 source/semantic subject 的原文、摘要和感人长改写；固定 forecast。
- **入口**：`langchao_runtime_adapter.build_shadow_round`；`langchao_reward.compile_candidate_reward`。
- **机械 oracle**：一个 goal/candidate/reward identity、一个 working-set slot、作用向量与 total cap 不变。
- **语义审查**：确认改写未偷换动作范围、对象、压力和回复义务。
- **正控**：真正从轻量分享改成索取即时回应时，必须新 semantic revision/重新评估。
- **现有测试**：`test_different_legacy_ids_with_same_semantics_share_stable_ids_and_one_slot`；`test_rhetorical_forecast_fields_do_not_affect_arithmetic`。
- **缺口**：现测以结构字段预先声明“同义”，没有真实文本双评审。

### T05｜跨用户材料隔离

- **fixture**：user A/B 各有关键词高度相似的私有资料；分别检索和构建 proposal。
- **入口**：`langchao_social.build_social_proposal`；`LangchaoSocialRepository.commit_proposal`；scope-bound repository reads。
- **机械 oracle**：异 scope source/build/resolver 必须拒绝；引用集合只含同 scope；词面相似度不得绕过。
- **正控**：同 scope 且 exact source ref 可正常引用，即使词面不相似。
- **现有测试**：`test_scope_is_strict_across_sources_items_and_build`；`test_resolver_must_match_scope_kind_and_id_exactly`；`test_blackbox_rejects_cross_scope`。
- **缺口**：应补真实 retrieval SQL/query recorder；目前结构层很强但未穷尽未来检索器。

### T06｜生成文字不等于实践完成

- **fixture**：task=`not_started`、artifact 空，render=`我研究完了`。
- **入口**：`LangchaoLiveRepository.settle_terminal`；`LangchaoOutcomeRepository`；未来 render scope checker。
- **机械 oracle**：goal 不完成；无 research-completed actual token；delivery 结算独立。
- **语义审查**：最终文本是否声称不存在的运行/产物；区分愿望、计划、尝试、完成。
- **正控**：真实 task success + artifact hash 后才允许完成 token，且 exactly once。
- **现有测试**：`test_success_settles_delivery_only_and_repeat_is_exactly_once`、`test_actual_and_correction_tokens_are_excluded_from_expected_ledger` 只提供局部原语。
- **缺口**：当前没有研究任务/产物领域合同，也未见 render 文本与 ledger 的完成声明复核器。

### T07｜计划—最终动作范围偏移

- **fixture**：计划 `expression.v1, asks_reply=false`；render 明确要求马上回复；用户负反馈 target=`reply_pressure`。
- **入口**：`LangchaoLiveService.execute_in_transaction`；`user_model_v2_labels.settle_labels`；语义评审器。
- **机械 oracle**：计划与最终文本都冻结入审计；负反馈归因实际动作或 UNATTRIBUTABLE，不更新原低压模板。
- **语义审查**：是否新增回复义务/催促；反馈对象是内容还是压力。
- **正控**：仍可忽略的轻量分享可保留原归因。
- **现有测试**：scoped negative 与 unknown judge 只属邻近证据。
- **缺口**：未见 plan-vs-render scope drift checker；这是明确实现缺口，不能用字符串骨架伪造通过。

### T08｜送达和接近分账、ACK 幂等

- **fixture**：同一 successful ACK 重放三次，窗口内无 reply。
- **入口**：`LangchaoLiveRepository.settle_terminal`；`settle_target_label`。
- **机械 oracle**：delivery actual 仅一次；reply/continuation/negative 不由 ACK 自动结算；窗口前 reply=PENDING，不造负反馈。
- **正控**：窗口内唯一可归因 reply 形成 positive label。
- **现有测试**：`test_success_settles_delivery_only_and_repeat_is_exactly_once`；`test_obs_01_pending_before_fixed_reply_deadline`；`test_obs_15_repeated_delivery_is_idempotent_by_key_and_revision`。
- **缺口**：两个模块分别覆盖，尚缺同一事务/窗口的全链 fixture 和 goal status 直接断言。

### T09｜有限事项正常完成

- **fixture**：finite goal，一次完整交付和完成 token，用户明确自然结束；30 秒与 6 小时两个 duration 变体。
- **入口**：`langchao_types.GoalContract`；`langchao_runtime_adapter._build_one`；`repeat_v2.evaluate_repeat_v2`。
- **机械 oracle**：两组均只完成/结算一次；完成后候选退休且不会因留存重开。
- **语义审查**：交付满足原范围，用户话语是自然结束而非不满。
- **正控**：删掉完成 token 则不完成；明确 REOPEN 才允许新 episode。
- **现有测试**：Goal DTO invariants、固定模板构造、`test_mot_12_user_reopen_resets_matching_identity`。
- **缺口**：`GoalStatus.COMPLETED` 可表示，但完成→退休→不重开的生命周期没有端到端证据。

### T10｜截断观察不是负样本

- **fixture**：6h exposure，2h 时 `observation_complete=false`，无观测。
- **入口**：`user_model_v2_labels.settle_target_label`；`expectations_v2.settle_expectation_target`。
- **机械 oracle**：reply=CENSORED/value=None；acceptance 不成为 observed negative；不写二元负样本。
- **正控**：完整 6h 无 reply 可结算 reply=false；acceptance 无显式反馈仍 UNKNOWN。
- **现有测试**：`test_obs_04_interrupted_partial_coverage_is_censored_not_zero`、`test_obs_02_complete_window_without_reply_is_binary_zero_not_busy_pseudolabel`、`test_acceptance_requires_explicit_attributable_feedback`。
- **缺口**：纯标签合同强，但缺 expectation residual/training repository 的同一链断言。

### T11｜双目标竞争、决定或暂缓

- **fixture**：contact/research 同时在 working set；冻结 readiness、attraction、edges、parameters、budget 与 tie order。
- **入口**：`langchao_engine.advance_langchao`。
- **机械 oracle**：候选共存；越阈值则按冻结规则决定；预算耗尽返回 `decision_budget_exhausted`；不删除目标或清零 readiness。
- **正控**：微增 contact/research 某一方 attraction，给足预算后该方可决定。
- **现有测试**：对称僵持、显式 tie rule、budget defer、同步 before snapshot、有界性。
- **缺口**：上层 contact/research 两类合法目标编译未串联；无限预算对称 stall 目前可能是 `decision=None,defer_reason=None`，缺 `competition_stalemate` 诊断。

### T12｜禁止优先于高准备度

- **fixture**：readiness=.95；permission v1 allow → v2 deny → v3 allow。
- **入口**：`build_shadow_round` 的 blocked drop；`LangchaoLiveService.execute_in_transaction` 的当前 boundary 重验；legacy `authorize`。
- **机械 oracle**：deny 期间无 claim/outbox；愿望无需清零；解除后从当前时点新 round 重审，不派发旧决定。
- **正控**：解除后有新用户请求或新 semantic revision 才能进入新执行。
- **现有测试**：blocked candidate drop、boundary 同义词阻断、render 后新边界撤回发送。
- **缺口**：缺 Langchao readiness/permission_version/working-set/claim 串联与“不补发”断言。

### T13｜反证、版本和依赖失效

- **fixture**：rev1 戏剧性旧解释，candidate C 依赖 exact hash；rev2 明确反证并 supersede。
- **入口**：`build_social_proposal`；`LangchaoSocialRepository.invalidate_source/commit_proposal`；`LangchaoLiveService._validate_contract`。
- **机械 oracle**：active pointer 转向新版本；旧依赖候选 invalidated/reassessment；live 不接受 stale contract。
- **语义审查**：新原文是否真推翻同一事项；不得偏爱更戏剧化旧证据。
- **正控**：无反证时 C 仍可竞争；无关证据不得误撤销。
- **现有测试**：`test_superseded_memory_produces_counterevidence_and_interpretive_basis`；`test_invalidation_advances_singleton_and_following_commit_uses_new_version`；`TestMemorySourcedInvalidation::test_a_superseded_memory_invalidates_the_candidate`。
- **缺口**：三个机制未串为同一 fixture，语义 supersedes 判定仍需 S。

### T14｜执行失败/送达未知不是用户拒绝

- **fixture**：决定已 commit；terminal failed、ACK absent/timeout、success 三分支。
- **入口**：`LangchaoLiveRepository.settle_terminal/pending`；live ACK wiring/recovery。
- **机械 oracle**：failed 只把 delivery 记 CENSORED；user outcomes 不变；unknown 保持可对账且没有第二 claim/outbox/send。
- **正控**：显式用户拒绝事件才生成 negative/rejection label。
- **现有测试**：failed ACK、不重复 settlement、restart recovery、invalid delivery 不能成为 no-reply sample。
- **缺口**：unknown/timeout 尚无一等聚焦 fixture，`pending()` 不自动等于“不盲重发”。

### T15｜四种“不发”原因分类

- **fixture**：固定其他输入，只切换 empty candidates、competition/budget defer、permission deny、selected+transport failure。
- **入口**：adapter、engine、live validation、live repository；审计 run/state。
- **机械 oracle**：分别得到 machine-readable `no_eligible_candidate`、`decision_budget_exhausted|competition_stalemate`、`permission_denied|boundary_blocked`、`delivery_failed`；决定/attempt 是否存在必须不同。
- **正控**：逐一移除单因素障碍，只恢复对应下一阶段。
- **现有测试**：四个局部机制各有测试；shadow replay 还钉住 defer reason agreement。
- **缺口**：reason 分散在 dropped reason、defer reason、exception 和 terminal kind，缺统一 taxonomy 与四链审计报告。

### T16｜权限按后续时序生效

- **fixture**：A allow@t1→deny@t2；B deny@t1→allow/revoke@t2；正负次数相同，并可逆序送达验证 occurred-at 政策。
- **入口**：`boundaries.detect_revocation/revoke/evaluate`；`BoundaryProjection.upsert`；`LangchaoState.permission_version`。
- **机械 oracle**：A denied、B allowed；不能平均成中性；active boundary 与最新有效事实一致；新 round 读取最终 permission version。
- **正控**：B 再加 deny@t3 变回 denied；不同范围 allow 不误解除。
- **现有测试**：revocation detection、reactivation upsert、carryover 不复活 revoked boundary。
- **缺口**：缺按原场景顺序交换的参数化验收，也未钉 projection version 到 Langchao state 的接线。

## 4. 本轮新增可执行 pytest 骨架

新增 `runtime/tests/test_acceptance_scenario_manifest.py`，仅测试现有能力，不写 `pass`、`xfail` 或伪语义断言：

1. **manifest 合同**：T01–T16 齐全，每项必须包含 fixture、入口、机械断言、语义评审、oracle、正控、现有测试、缺口、证据。
2. **T01 机械子合同**：busy observed 与 unknown missingness 分离；unknown outcome 不成为负收益。
3. **T10 机械子合同**：两小时截断=CENSORED；完整六小时无 reply=OBSERVED_NEGATIVE。
4. **T11 机械子合同**：预算耗尽明确 defer 且保留两个目标；给足预算的正控能决定。

没有给 T02/T06/T07/T09 等缺实现的项造空测试；没有用字符串规则替代 T01/T04/T06/T07/T09/T13 的语义评审。

## 5. 建议最小整改顺序

1. **先补真实性边界**：T02 artifact witness，T06 task/artifact completion contract，T07 plan-vs-render executed-action contract。
2. **再补生命周期**：T09 finite completion/retire/reopen，T12 permission change/new round/no backlog，T14 unknown delivery reconciliation。
3. **统一审计原因**：为 T15 建跨层 reason taxonomy，并让 no candidate、stalemate/defer、permission denial、delivery failure 都能在一个报告中区分。
4. **补历史时序**：T16 使用 event occurred-at + projection version 的双序列 fixture。
5. **语义证据单独交付**：T01/T04/T06/T07/T09/T13 保存原文、rubric、双评审分歧与一致率；机械 pytest 不代替 S。
