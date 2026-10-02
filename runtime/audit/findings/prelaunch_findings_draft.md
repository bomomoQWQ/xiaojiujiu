# 浪潮上线前审查问题单（草案）

> **文档性质**：问题单，不是最终报告；不构成上线授权。  
> **当前姿态**：`hold_not_authorized`。  
> **基线**：`feat/langchao-decision-engine` / `32bd8a2fbad167c3f6c66d99e704268208d5fed5` / DB v20。  
> **依据**：purpose registry、static call-chain/consumers、T01–T16 场景映射、预注册 acceptance manifest、有限 deterministic T tranche。  
> **限制**：本草案没有 R（授权真实运行）证据；语义 S 也未完整交付。测试文件存在只算 C，只有保存的执行结果才算 T。

## 1. 分级与证据口径

### 优先级

- **P0**：上线阻断。涉及硬安全、真实性、删除/用户控制或实际动作归因。
- **P1**：广泛 live authority 前必须修复。涉及闭环、路由、证据完整性或审计可追溯性。
- **P2**：有限上线约束。必须禁用、明确标注或限定范围，不得夸大能力。
- **P3**：不阻断受限上线的后续改进。本轮指定问题中没有 P3。

### 证据

- **D**：版本化设计/预注册要求，只证明“应当如此”。
- **C**：本轮读取的源码和静态调用链，只证明实现或断链。
- **T**：保存输入、命令与结果的隔离执行，只支持实际执行的 tranche。
- **R/S**：本草案不声称已有完整真实运行或人工语义证据。

## 2. 汇总

| ID | 级别 | 问题 | 当前最强证据 | 上线处置 |
|---|---|---|---|---|
| PF-001 | **P0** | rendered/sent text 未进入训练曝光事实 | D/C3 | 阻断 |
| PF-002 | **P1** | social 子系统生产未接线 | C2 + C3 断链；局部 T | 修复或明确禁用 |
| PF-003 | **P1** | actual user outcome 未回灌收益闭环 | D/C3；局部 T | live 前修复 |
| PF-004 | **P2** | attention 固定 all-one | D/C3 | 标注为常量或实现动态生产者 |
| PF-005 | **P2** | competition_gain=0 | D/C3；核级 T | 仅可声称无竞争简化模式 |
| PF-006 | **P1** | shadow authority 路由不跑 shadow | D/C3 | 修复 mode 语义 |
| PF-007 | **P0** | 删除策略无跨派生物执行闭环 | D/C 部分；无闭环 T | 阻断 |
| PF-008 | **P0** | T02 artifact witness 缺口 | D/C | 阻断 |
| PF-009 | **P0** | T06 伪完成声明缺口 | D/C；局部 T 不足 | 阻断 |
| PF-010 | **P0** | T07 plan→render 范围漂移缺口 | D/C | 阻断 |
| PF-011 | **P2** | T09 完成→退休→不重开缺口 | D/C | 限定能力 |
| PF-012 | **P1** | Txx 编号在审查工件间语义漂移 | C | 先统一 crosswalk |

合计：P0=5，P1=4，P2=3，P3=0。

---

## PF-001｜P0｜最终 rendered/sent text 未进入训练曝光事实

**审查问题**：成功发送后，训练曝光是否能证明并区分实际发送文本，而不是只复用渲染前冻结的计划 action shape？

**当前答案**：否。`rendered_text` 进入 `action_attempts` 并用于发送前授权，但 ACK→exposure 使用 committed `chosen.action`；feature encoder 不读取最终文本、哈希、长度、语气或模板版本。同一 action shape 下不同措辞对训练不可区分。

**风险**：计划动作与实际措辞发生范围、压力或语义漂移时，反馈会错误归因给原计划模板，违反“按实际动作学习”。

**证据**：

- **D**：`runtime/audit/scenarios/T01-T16.json` 的原始场景 T07；acceptance manifest 的“按实际动作而非计划动作学习”要求。
- **C**：`static_call_chain_review.md:153-174,198`；`static_consumers.json:282-294`；`api_v1.py:1397-1410,1831-1857`；`runtime_v2.py:845-880`；`user_model_v2_features.py:217-258`。
- **T**：无修复后端到端 T。

**最小整改**：

1. 成功 ACK 时从 exact attempt 冻结 actual-action witness，至少含 `rendered_text_sha256`、规范化长度/结构特征、render/template/encoder version，并绑定 attempt/outbox/exposure。
2. 若训练消费文本派生特征，预测时必须可获得同定义特征，避免 post-treatment leakage；不可获得时反馈标 `unattributable/unknown`。
3. 删除 `SendAckV2.action` 死字段，或强校验它与持久化 actual-action witness 一致。

**正向回归**：

- 两条计划 action 完全相同、最终文本不同：修复后 provenance/hash 必须不同，训练样本不可再完全相同。
- 负控：`asks_reply=false` 被渲染成催促立即回复，反馈不得更新原低压模板。
- 正控：最终文本与计划等价且 witness 完整时，合法 exposure/label/训练路径仍可达。

**退出条件**：actual-action witness 不可变绑定；负控/正控有保存的 T；语义漂移另补 S。

## PF-002｜P1｜social 子系统实现存在但生产未接线

**审查问题**：生产 composition 是否能从真实、同作用域 social source 生成并消费带 exact refs 的候选？

**当前答案**：否。builder/repository/resolver 未在 production composition 实例化，`build_social_proposal` 无生产调用；legacy action 不携 `social_ref`，expression 候选常被丢弃。

**证据**：

- **D**：设计中的来源、失效和删除依赖要求。
- **C**：`static_call_chain_review.md:137-151,203`；`static_consumers.json:297-307`。
- **T**：deterministic tranche 只支持 social projection source validation，不支持 production composition。

**最小整改**：

1. composition 显式构造 scope-bound resolver、builder 与 repository；若本期不上线则禁用并删除“已接入”宣称。
2. 候选 action 携 exact `social_ref/memory_ref`（scope/id/revision/hash），进入 contract refs 与 live revalidation。

**正向回归**：同 scope 有效 source 全链可达；跨 scope/tombstoned/hash 变化无 claim/outbox；接线后合法 expression 不再因缺 ref 被误丢。

## PF-003｜P1｜actual user outcome 未回灌浪潮 outcome/reward 闭环

**审查问题**：reply/continuation/negative 的真实用户观测是否结算浪潮 actual/correction token，并影响后续 attraction/readiness？

**当前答案**：否。live ACK 只结算本地执行结果；v2 labels 不回灌浪潮 ledger，actual ledger 也无 future reward/state consumer。切到 langchao/live 后，发送还不创建 v2 exposure/labels/repeat metadata。

**证据**：

- **D**：purpose registry 中用户结果与收益目的。
- **C**：`static_call_chain_review.md:120-135,196-204`；`static_consumers.json` 的 G3/G5、actual outcome 与 attraction 项。
- **T**：现有 T 只证明本地 terminal settlement/idempotency，不证明用户结果闭环。

**最小整改**：

1. langchao/live 成功发送创建与 v2 等价的 exposure/pending labels/repeat metadata。
2. 将 settleable 用户观测映射为浪潮 actual/correction token，或确立唯一共享事实账并让后续 reward compilation 消费。
3. 补 continuation=true、negative=true 等目标生产者；无法补则从可学习/可声称目标中移除。

**正向回归**：commit→render→send→ACK→reply/continue/negative→v2 label 与浪潮 token→下一轮 reward diff；重复事件幂等；无反馈时 delivery 可结算但用户结果保持未知。

## PF-004｜P2｜attention 固定 all-one，尚非动态信号

**当前答案**：生产 adapter 固定八方向全 1，版本 `langchao.attention.all-one.v1`；只有结构和审计字段，没有动态生产者。

**证据**：D=`purpose registry: attention_weights`；C=`langchao_runtime_adapter.py:43,277-279,486-495`；无动态语义 T。

**最小整改**：上线材料统一标注 `constant/not-yet-informative`。本期不实现则锁定版本和全 1 不变量；若实现则登记来源、更新频率、权限、审计 hash 与 fail-closed 规则。

**正向回归**：常量模式算术不变；动态模式拒绝缺轴/负值/非有限值/未入 audit hash 的版本；合法 attention 只改变对应 weighted term，不得改变概率、权限或硬边界。

## PF-005｜P2｜默认 competition_gain=0，完整竞争语义未启用

**当前答案**：冻结配置和默认 wiring 都是 `competition_gain=0.0`。数值核支持非零竞争，但生产配方实际为无竞争简化模式。

**证据**：D=预注册 B0–B3 与冻结配置；C=`langchao_shadow_wiring.py:37-45`、purpose registry readiness；T 只支持核级有界积分，不证明生产非零竞争。

**最小整改**：当前只能声称 competition-disabled/B2-like。若启用竞争，必须有批准的 parameter revision、分层指标、回滚条件和多候选 fixture，不得直接改常量上线。

**正向回归**：gain=0 结果保持；非零 gain 下竞争项可复算、budget defer/tie/stalemate 可观察；微增一方 attraction 后合法正控仍可越阈值。

## PF-006｜P1｜`langchao/shadow` authority 路由不运行 shadow runner

**当前答案**：独立 shadow authority 只走 `v2.assess_endogenous`；shadow runner 仅作为 runtime_v2/live 附件或 live evaluator 使用。

**证据**：D=shadow coverage/零副作用目的；C=`static_call_chain_review.md:57-68,199`、`static_consumers.json` runtime mode、`langchao_live_wiring.py:114-146`；无 truth-table T。

**最小整改**：二选一：在该分支调用 shadow runner并维持零副作用；或删除/重命名这一 authority mode，明确 shadow 仅是附件。

**正向回归**：shadow authority 产生 witness 且 sent/reward/training/quota/outbox delta 全 0；runtime_v2/live 附挂 shadow 不影响基线发送；none/disabled 无 shadow 写入。

## PF-007｜P0｜删除策略无跨原文、投影、候选、训练与审计派生物的执行闭环

**审查问题**：用户删除来源或退出时，是否有统一控制入口清理/失效所有直接与派生数据，并限定最小审计保留？

**当前答案**：只有局部 social tombstone/dependency invalidation。系统多处采用 append-only/archival，未见覆盖 exposure、features、labels、training provenance、parameters、prediction/shadow/review artifacts 的统一删除传播执行器。预注册只有政策声明。

**证据**：

- **D**：设计 `452,1485-1487,S04`；manifest `privacy.deletion_propagation`；semantic rubric 删除派生统计要求。
- **C**：`langchao_social_repository.py:540-616,746`；`memory.py`/`api.py` 的 archival-not-deletion；T17–T32 映射 T20/T28。
- **T**：局部 social validation，不是跨存储删除传播 T。

**最小整改**：

1. 定义版本化 deletion/retention contract：触发主体、scope、数据类别、硬删/去标识/tombstone、最小审计字段、期限、重试。
2. 建跨存储传播清单：原始消息、memory/social、projection/candidate/contracts、exposure/features/labels、training provenance/parameters、prediction/shadow/review artifacts。
3. 删除后依赖候选立即失效并在 live 前重验；保留哈希须证明不可反推且有期限。

**正向回归**：删除后检索和训练集均不含来源，依赖候选不可执行；传播重试幂等且跨 scope 不受影响；未删除合法数据仍可使用，最小审计事件不含原文或可恢复特征。

## PF-008｜P0｜T02 缺真实能力与 artifact witness 联合门禁

> 本项的 T02 指 `scenarios/T01-T16.json` / `T01-T16_REVIEW_MAPPING.md` 中“检索能力与真实产物门禁”。

**当前答案**：live 只校验声明式 capability/source refs，不证明检索执行或 artifact 存在。

**证据**：D=原始场景 T02 与 fabricated artifact 硬阻断；C=`T01-T16_REVIEW_MAPPING.md:48-55`、`langchao_live.py:155-200`；T 缺失。

**最小整改**：新增 artifact registry/witness，绑定 task/run、capability、scope、artifact hash、状态与时间；claim 前 exact 校验，缺失/失败/跨 scope/hash 不符均 fail closed。

**正向回归**：missing capability/artifact、failed task、hash mismatch 均无 claim/attempt/outbox；正控为同 scope 成功 task + 可解析 hash-bound artifact 保持候选可达。

## PF-009｜P0｜T06 缺 render 完成声明与 task/artifact ledger 复核

> 本项的 T06 指原始场景“生成声明不等于实际研究完成”。

**当前答案**：分账可防 ACK 自动结算研究结果，但没有检查“我研究完了”是否有 task success 与 artifact witness。

**证据**：D=原始场景 T06/真实性硬阻断；C=`T01-T16_REVIEW_MAPPING.md:85-93` 与 render→send 链；T 只覆盖 delivery settlement separation，不覆盖文本真实性。

**最小整改**：定义完成声明类型及 witness requirements；authorize 阶段缺 witness 时 rewrite/reassessment；完成 token 只能由 task success + artifact hash exactly-once 结算。

**正向回归**：未执行却声称完成必须拒绝/改写；真实 task+artifact 正控可发送并结算一次；双评审区分愿望、计划、尝试、完成。

## PF-010｜P0｜T07 缺 plan-vs-render 动作范围漂移门禁

> 本项的 T07 指原始场景“计划与最终动作范围偏移”。

**当前答案**：最终文本有一般边界授权，但未见 plan/candidate 与 render 之间针对对象、压力、回复义务、承诺、产物声明的语义漂移比较。

**证据**：D=原始场景 T07 与 manifest 中 render scope/actual-action 要求；C=`T01-T16_REVIEW_MAPPING.md:95-103`、`api_v1.py:1831-1857`；T/S 均缺。

**最小整改**：冻结 plan scope 与 rendered scope；扩大范围必须 rewrite 或生成新 semantic revision 重新过 permission/reward/boundary，旧 claim 不可复用；训练绑定 actual revision。

**正向回归**：轻量分享→催促回复不得直接发送；等价低压改写正控保持可达；双评审保存是否新增回复义务及分歧。

## PF-011｜P2｜T09 有限目标完成→退休→不重开生命周期缺口

> 本项的 T09 指原始场景“有限事项正常完成”。

**当前答案**：`GoalStatus.COMPLETED` 与 REOPEN 原语存在，但没有完成→退休→不重开全链证据。

**证据**：D=原始 T09、设计中“删除/放下不算完成”；C=`T01-T16_REVIEW_MAPPING.md:114-122`、`repeat_v2.py:183-276`；T 缺失。

**最小整改**：实现 finite goal terminal transition 与 candidate retirement；只允许显式用户 REOPEN 或新 semantic episode 重开；删除/降低重视/关闭关注不得领取完成收益。

**正向回归**：30 秒/6 小时变体均只结算一次且不复活；显式 REOPEN 正控创建新 episode；无完成 token 不得完成。

## PF-012｜P1｜T02/T06/T07/T09 编号在审查工件间语义漂移

**当前答案**：同一裸 Txx 在不同工件指不同场景。例如 scenario map T02 是“检索能力与产物真实性”，manifest T02 是“作用域与私人材料隔离”；T06/T07/T09 也发生标题和 oracle 偏移。

**风险**：可能对错 oracle，形成“T09 已通过”但实际是另一场景的证据串线。

**证据**：C=`scenarios/T01-T16.json`、`T01-T16_REVIEW_MAPPING.md` 与 `langchao_prelaunch_acceptance_20261002.json` 的对应条目直接对照。

**最小整改**：冻结 canonical scenario id（族前缀或 UUID），给旧编号显式 crosswalk；更新 manifest、fixture 路径、问题单和结果汇总，历史工件用 superseding revision，不覆写。

**正向回归**：schema 保证一个 canonical id 只对应一个 title/oracle/fixture hash；旧编号可唯一解析；历史 evidence 仍按原 revision 可追溯。

---

## 3. 建议最小整改顺序（非最终结论）

1. **先解决真实性与用户控制 P0**：PF-007 删除闭环；PF-008/PF-009/PF-010 artifact、完成声明、动作漂移；PF-001 actual-action learning。
2. **再修证据与路由 P1**：PF-012 canonical ID/crosswalk；PF-006 shadow authority；PF-002 social composition；PF-003 user outcome 回灌。
3. **最后决定 P2 的上线口径**：PF-004 attention 明确为常量；PF-005 competition-disabled；PF-011 finite-goal 生命周期未完成前禁用相应能力宣称。
4. 每个负向门禁必须带上文列出的**正向回归**，避免用“全不发/全不学/全删除”取得伪安全通过。

## 4. 草案结语

本问题单仅把已知缺口转成可整改、可回归、可追溯的上线前问题。当前 P0 未关闭，且缺少 R 与完整 S，因此不得将本文件解释为最终报告、通过证明或上线授权。
