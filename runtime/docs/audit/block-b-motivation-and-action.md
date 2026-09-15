# 设计文档 §32–§53 对照实现（动机与行动区块）

把 `内源主动型长期陪伴AI_Runtime_完整架构设计.md` 的 **§32–§53**（文档第 1652–2309 行：接近动力学 / 候选意图 / 动机博弈 / 边界状态机）逐条对照 `runtime/src/companion_runtime/` 的实际代码。

**本次审计只读代码，未修改任何源码、测试或文档；唯一的写入物是本文件。**

| 项目 | 值 |
|---|---|
| 审计对象 | `F:\理解痞老板\内源主动型长期陪伴AI_Runtime_完整架构设计.md` §32–§53（`第 1652–2309 行`） |
| 补丁（判定依据） | `F:\理解痞老板\PATCH_v0.2_即时演出与持久认知分离_移除本地2B核心依赖.md`（1352 行） |
| 代码根 | `runtime/src/companion_runtime/` |
| 审计日期 | 2026-09-15 |
| 审计时 HEAD | `8564327`（`docs: 把插件市场发布与加 CI 标记为已冻结`，2026-09-15） |
| 工作区状态 | **脏**：审计对象是**工作区当前内容**，不是 `8564327` 的树。`git status --porcelain` 显示 `M runtime/pyproject.toml`、`M src/companion_runtime/{config,db,maintenance,projections,runtime}.py`、`?? src/companion_runtime/{db_base,db_postgres}.py`、`?? tests/{test_db_postgres,test_sql_portability}.py` |
| 审计方式 | 只读：`read` / `grep` 源码 + `git rev-parse` / `git status` / `Get-FileHash`。未运行测试，未修改任何文件 |

> **并发改动提醒。** 审计期间工作区被其它进程继续修改（审计开始时 `git status` 6 项；中途变成 10 项，多出 `maintenance.py`、`db_postgres.py`、`test_db_postgres.py`；结束时 12 项，又多出 `tests/test_api_reliability.py`、`framework/cf/onebot*.py`，以及本报告新增的 `runtime/docs/audit/`）。因此下表另附**审计时文件哈希**；行号指向该哈希对应的内容，若文件已被改动需重新核对。

| 文件 | SHA256(前 12) |
|---|---|
| `motivation.py` | `8A3CE2E8CF03` |
| `candidate.py` | `0EDA5B4EB5AD` |
| `boundaries.py` | `AF108A8EC100` |
| `utility.py` | `4655C7D962A8` |
| `action.py` | `6A2DAB348937` |
| `authorize.py` | `452CDAA257F3` |
| `reducer.py` | `8F2CDA613D11` |
| `runtime.py` | `DB5C3E286C08` |
| `config.py` | `AD6C2DF0AD73` |
| `typing.py` | `5AD5E56355A1` |
| `pool.py` | `A863F3DEE815` |
| `deep_refresh.py` | `CFD0DF38426B` |
| `providers.py` | `146BD6F019B8` |
| `user_model.py` | `4C255BD06461` |
| `api.py` | `C9908014A5CD` |
| `delivery.py` | `7BF419285F32` |
| `projections.py` | `7434C0B16BFE`（审计**结束**时取；该文件在审计期间被并发修改，所引用的 539 / 560 / 758 / 834 / 858 / 962 行已在结束时逐行复核仍成立） |
| `db.py` | `FE415E8AF615`（同上；所引用的 64-66 / 216-228 行已在结束时复核仍成立） |

审计结束时（第二次取哈希）上表 16 个文件与开始时完全一致，即**本报告引用的代码在审计期间没有漂移**；工作区其它变动（`maintenance.py`、`db_postgres.py`、`test_*`、`framework/cf/*`）不在本区块引用范围内。

## 判定图例

| 判定 | 含义 |
|---|---|
| **已实现** | 文档要求的每一条都在生产代码里被执行（有真实调用方，不靠配置项 / 注释 / 测试占位） |
| **部分实现** | 主体在位但形状不同或链路不完整；每条都写清**差在哪** |
| **未实现** | 只有配置项、字段、注释或测试，没有任何生产路径消费 |
| **被补丁取代** | 补丁 v0.2 已改写该要求，按补丁判定，不计为缺失 |
| **文档·约定** | 该节本身是命名 / 哲学 / 约定，没有可写的代码落点 |

### 补丁对本区块的影响（结论先行）

**§32–§53 没有任何一节被补丁整体取代。** 补丁 §22 明确把「I-R-P / 边界 / 未尽之事 / 协议 / **动机决策**」划在 Level 0（确定性 Runtime），把「候选意图」划在 Level 2（低频、**非必需**）；补丁 §18/§19/§20/§21 细化了候选意图生成的位置（深层认知刷新）与触发条件。因此：

- §32–§36、§43–§53：补丁未改，按原文判定；
- §37–§42：生成器的**位置**被补丁从「每轮强 API」改成「低频深层刷新 / 可选 provider」，输入清单被补丁 §19 取代为 `DeepRefreshRequest` 的 9 个字段——代码实现的正是补丁那一版，所以本表 §38 的差异是「对设计原文」的差异，不是对补丁的违背。

---

## 一、逐章节对照

| 章节 | 要求要点 | 代码位置 | 判定 | 差在哪 / 证据 |
|---|---|---|---|---|
| §32 | 「病娇值」拆成 I(t) 接近冲动、R(t) 节制、P(t) 积累压力，且 0 ≤ I,R,P ≤ 1 | `typing.py::RuntimeState`(315-317)、`motivation.py::step_drives`(467-479)、`motivation.py::release_after_contact`(510-512)、`db.py::runtime_state`(64-66) | 已实现 | 三个变量是 `runtime_state` 的独立列（`approach_impulse` / `restraint` / `pressure`，默认 0.05 / 0.50 / 0.0），每一次写都经过 `clamp()`；`step_drives` 与 `release_after_contact` 是唯一的更新点，`lazy_tick → _apply_time_passage`（`runtime.py:514`）在真实路径上执行 |
| §33 | 输入 x(t) = [E, O, M, A, C, B, W, U, …]（8 个具名分量） | `motivation.py::DriveInputs`(354-371)、`runtime.py:642-652` | 已实现 | 8 个分量逐一对应且都有真实赋值：E=`_emotion_tendency`、O=`unfinished.priority_of`、M=`memory_store.activation_strength`、A=`_hours_since_exchange`、C=`recent_contact_ratio`（近窗主动次数/容忍度）、B=`boundary_pressure`、W=`user_model.busy_probability`、U=依 `effective_count` 取 0.35 或 0.15（`runtime.py:650`，即**两档代理**而非连续不确定性，见「可以永远不做的」） |
| §33 | 目标冲动 Î = σ(θ_Iᵀx+b_I)、目标节制 R̂ = σ(θ_Rᵀx+b_R) | `motivation.py::target_drives`(386-437) | 已实现 | `impulse_logit`(406-422) / `restraint_logit`(423-433) 是线性式加偏置（−1.60 / −0.40），再过 `sigmoid`；θ 由 `state.values` 编译（`user_care`、`boundary_respect`、`relationship_maintenance`、`curiosity`、`stability_commitment`、`conflict_directness`），缺席项由 `absence_term`（36h 饱和）主导 |
| §34 | dI/dt=(Î−I)/τ_I、dR/dt=(R̂−R)/τ_R；τ_I 控制「上头速度」 | `motivation.py::step_drives`(465-470) | 已实现 | 解析一阶步进 `alpha = 1−exp(−dt/τ)`，跨小时缺席一次 lazy tick 也不累积误差；τ_I=5400 s、τ_R=9000 s（`config.py:139-140`，文档未给数值），τ_R>τ_I 即「节制比冲动慢」 |
| §35 | dP/dt = κ₊(1−P)S_β(I−R) − κ₋P·S_β(R−I) | `motivation.py::step_drives`(472-479) | 已实现 | 代码逐项一致：`accumulation = kappa_plus*(1−P)*softplus(gap)`、`dissipation = kappa_minus*P*softplus(−gap)`，`P += (acc−diss)*effective_dt`。**差异**：积分步长被 `effective_dt = min(dt, max_pressure_step_seconds=21600)` 截断（`motivation.py:478`，`config.py:153`），文档的 ODE 没有这个上限 |
| §35 | S_β(x) = ln(1+e^{βx})/β | `utility.py::softplus`(55-68) | 已实现 | `softplus` 的 docstring 直接写着同一式子；数值上对 ±40 做了分支避免溢出，语义不变；β 由 `DriveConfig.beta=4.0` 传入 |
| §36 | I(t_a⁺)=(1−ρ_I)I(t_a⁻)、P(t_a⁺)=(1−ρ_P)P(t_a⁻)、R(t_a⁺)=min(1,R(t_a⁻)+ρ_R) | `motivation.py::release_after_contact`(510-512) | 已实现 | ρ_I=0.55、ρ_P=0.70、ρ_R=0.06（`config.py:144-146`）；**执行时刻是 commit 而不是 send**——调用点在 `runtime.py:1352`（`_commit_attempt` 内），见缺陷 D5 |
| §36 | 「进入冷却」 | `motivation.py::release_after_contact`(513-514)、`cooldown_remaining`(542-546)、`runtime.py::_recent_contact_count` | 已实现 | `cooldown_until = max(旧值, now+2400s)`，冷却期间候选被 `decide` 标为 `cooldown_active` 直接出局（`motivation.py:620-622`），投递授权也会再拦一次（`authorize.py:158-165`） |
| §37 | 候选意图生成器 ≠ RAG ≠ 动机层（想起什么 / 可能想做什么 / 到底做不做） | `candidate.py`（模块 docstring 与 `generate`）、`memory.py`（RAG）、`motivation.py::decide` | 已实现 | 三个问题分在三个模块，`candidate.generate` 只产出候选、`decide` 只做博弈；`candidate.py:5` 明确写出这条分工 |
| §37 | 生成器本体：由低频强 API 回答「知道这些以后我现在可能想做什么」 | `candidate.py::generate`(178-256，规则路径)、`deep_refresh.py::ground_suggestions`(279-359)、`reducer.py::_apply_deep_refresh`(641-646) | 部分实现 | 默认配置下**只有规则生成器在跑**：`generate` 只会产出 `follow_up`（未尽之事）/ `curious_question`（激活记忆）/ `contact`（永久候选）三种形状，`validate_candidate` 白名单里的 `share` / `repair` / `reply` 没有任何生成器产出；模型生成那条路存在（`candidate_intent` 操作 → `_apply_candidate_operations`），但**没有任何内部代码派发 `TaskKind.CANDIDATE_GEN`**（全仓仅 4 处引用：`typing.py:214`、`protocol.py:54`、`reducer.py:396`、`reducer.py:428`，全是枚举定义或处理端），且默认 `semantic.provider="disabled"`（`config.py:364`）使 `Runtime.deep_refresh` 在 `provider_unavailable` 处直接返回（`runtime.py:1423-1426`） |
| §38 | 生成器输入五块：相关过去 / 关键用户原文 / 当前工作局势 / 当前内部状态 / 已有候选池 | `deep_refresh.py::build_request`(409-464)、`providers.py::DeepRefreshRequest`(134-158) | 部分实现 | 五块都有对应字段（memories+unfinished / key_quotes / situation / mood+active_emotions / candidates），但这是**补丁 §19 的刷新输入**，与设计 §38 的清单有两处实质差别：**缺「近期执行过的意图」**（`action_attempts` 从未进入请求体），**「激活记忆」被换成 `list_memories(limit=8)`**（`deep_refresh.py:459`，按 importance 排序，不是激活池——`providers.py:143` 的 docstring 却写着 "Activated memories"，见缺陷 D8） |
| §38.1 | 相关过去只放「激活记忆 / 未尽之事 / 近期执行过的意图」，不要整段历史 | `deep_refresh.py:443-460` | 部分实现 | 「不要整段历史」成立（请求体不带 transcript，`deep_refresh.py:388-389`）；三项里「近期执行过的意图」缺失（见上），「激活记忆」实为重要性 Top-8 |
| §38.2 | 关键原文保留少量 char/user 原话，给强模型纠正上游摘要的机会 | `deep_refresh.py:420-432`（`key_quotes`，默认 3 条）、`providers.py:147` | 部分实现 | 机制在位且被测试钉住，但原文只从**未结算（unresolved）事件**里取（`deep_refresh.py:409-421`），已被粗粒度结算的普通轮次原话进不去；设计要的是「给强模型纠错机会」的那几句关键原话，实际范围比设计窄 |
| §39 | 输出 ADD / UPDATE / RETIRE / REINTERPRET，不做整池重建 | `typing.py::CandidateOp`(182-188)、`candidate.py::CandidateOperation`(49-91)、`pool.py::apply_one`(48-157) | 已实现 | 四种 op 各有落地实现：`add`(76-88)、`update`(90-119，带可写字段白名单)、`retire`(121-136)、`reinterpret`(138-155，写成 `interpretations` 新版本而不是改写候选)；未知名直接 `ValueError`（`pool.py:157`），批量应用时逐条 savepoint 拒绝而不炸整批（`pool.py:185-195`） |
| §40 | 候选字段：type / intent / goal / target / sources / constraints / preconditions / invalidate_when / confidence | `typing.py::CandidateIntent`(618-639) | 已实现 | 九个字段全部存在，另有实现附加字段（status / internal_need / unfinished_relevance / emotion_relevance / created_at / updated_at / expires_at / retired_reason / proposed_by）；`pool.py:96-111` 明确列出外部可写字段集 |
| §40.1 | `goal` 很重要：相同行为来自不同动机，**动机不同则后续效用完全不同** | `typing.py::CandidateIntent.goal`(624)、`candidate.py`（各生成器写死 goal）、`action.py::render_payload`(512-521)、`runtime.py:1369` | 部分实现 | `goal` 一路传到行动尝试与渲染 payload（渲染端能看到「了解后续进展并表达关心」），但**它不参与任何效用计算**：`motivation.candidate_utility`(150-266) 读的是 `internal_need`、`unfinished_relevance`、`type`、`prediction`，`candidate.goal` 与 `candidate.constraints` 在整个函数里都没出现。设计说「动机不同，后续效用完全不同」，代码里换一个 goal 不会改变 U_i 一个比特 |
| §40.2 | sources 不能凭空出现，可取 unfinished / memory / emotion / situation / internal_approach_drive | `candidate.py:40-46`（五个前缀常量）、`validate_candidate`(358-378)、`runtime.py::_event_ids_behind`(1212-1261)、`runtime.py::_is_resolvable`(1651-1693) | 部分实现 | 强制非空 sources 成立（`missing_sources` 拒绝），三类有真实产出者（`unfinished:` / `memory:` / `internal_approach_drive`）；**`emotion:` 与 `situation:` 两个常量没有任何产出者**（`generate` 从不写它们），只在 `_event_ids_behind` 里被当作「不指向事件」处理；另外两条来源命名约定没有对齐：规则生成器写的是**带前缀**的 `unfinished:<id>` / `memory:<id>`（`_event_ids_behind` 认这个形式），而深层刷新的 grounding 闸门 `_is_resolvable` 只认**裸 id**（`evt_` / `mem_` / `cnd_` / `mcd_` / 裸 `unf_` 与 `emo_`），带前缀的 source 会被判为 `ungrounded_sources` 丢弃，见缺陷 D13 |
| §41 | 生命周期 new → active ↔ dormant → resolved / retired / expired | `typing.py::CandidateStatus`(136-148)、`projections.py::CandidateProjection`(940-1026)、各 `set_status` 调用点 | 部分实现 | `new`/`active`/`resolved`/`retired`/`expired` 都有真实写入路径（commit→active `runtime.py:1383`，回复满足→resolved `runtime.py:1960`，用户回复归因→resolved `runtime.py:1774`，未尽之事结算→resolved `runtime.py:2133`，invalidate→retired `2155`，TTL→expired `2116`，池操作 retire `pool.py:128`）；**`dormant` 没有任何写入者**，它只出现在「哪些状态算可用」的查询里（`projections.py:962`），所以文档的 `active ↔ dormant` 这条边等于不存在 |
| §41 | `committed` 不属于候选生命周期；一旦决定执行，创建独立的**行动尝试** | `typing.py::CandidateStatus`（无 committed）、`action.py::create_proposal`(117-143)、`runtime.py::_commit_attempt`(1321-1386) | 已实现 | 候选状态枚举里没有 `committed`；决定执行时 `create_proposal` → `commit`（`action.py:146-180`）建立独立 `ActionAttempt`，并把候选置为 `active`（`runtime.py:1382-1384`），行动尝试自带 9 状态机（`action.py:34-67`） |
| §42 | 强 API 没有直接写数据库权，只能提四种操作，真正执行由候选池管理器完成 | `pool.py`（模块 docstring 与 `apply_operations`）、`runtime.py::apply_candidate_operations`(2182-2225)、`api.py:764-774` | 已实现 | 唯一入口是 `apply_candidate_operations` → `pool.apply_operations`，且写入发生在 `Reducer` 的单一写者事务内；模型侧只能给 op 列表（HTTP `/candidates/operations` 或 `CANDIDATE_GEN` / `DEEP_REFRESH` 提案），`pool.py:189` 对坏操作只拒绝不崩溃 |
| §42 | 工作局势每次变化后：检查 active 候选 `invalidate_when`，失效则退役 | `runtime.py::_invalidate_candidates`(2139-2159)、`candidate.py::invalidated_by_situation`(439-460)、`_condition_keywords`(463-474) | 部分实现 | 检查确实执行，但只在**用户消息入口**这一条路上（`runtime.py:1023`），而工作局势在投递路径也会被写（`reducer.py:1718`，投递成功后写入「我主动联系了用户…」），那一处不会触发检查；更关键的是条件匹配靠一张**四条硬编码关键词表**（`candidate.py:465-470`），表外的任何 `invalidate_when`（包括模型自己写的自然语言条件）永远匹配不上而静默失效，见缺陷 D4 |
| §43 | 永久保留特殊候选「没有明确事项，只是想和用户建立联系」 | `candidate.py::generate`(229-254)、`CONTACT_CANDIDATE_TYPES`(40) | 已实现 | 每次池刷新（`should_refresh`：池空 300 s / 非空 900 s，`config.py:274-275`）只要没有存活的 `contact` 候选就重建一条；intent 文案与文档仅差一词（代码 `candidate.py:237` 是「没有**具体**事项，只是想和用户建立联系」，文档是「没有**明确**事项」）；它带 6h TTL（`default_ttl_seconds=21600`）但因每 900 s 重生成而实际常在；`is_candidate_proactive` 把它算作主动候选，因此受硬边界管辖 |
| §43 | V_contact = σ(aI + bP − cR)：冲动低压力低则几乎没价值，冲动高压力高则自然爬升 | `candidate.py::contact_candidate_value`(99-120) | 部分实现 | a=2.6、b=1.4、c=0.9（`config.py:281-283`）与文档同号同构，并被真实消费（`confidence` 与 `internal_need`，`candidate.py:244-246` → `candidate_utility` 的内部收益与 urgency 项）；**差异**：代码在 σ 内多一个中性先验 `contact_baseline_prior = −0.25`（`config.py:280`），文档式子里没有这个 B0，所以「冲动压力都为 0」时文档算 σ(0)=0.5、代码算 σ(−0.25)=0.438 |
| §44 | 动机 / 博弈决策层决定「到底做不做」 | `motivation.py::decide`(560-698)、`runtime.py:1166-1186` | 已实现 | 每一次内源轮次都会组装 `MotivationInputs` 并调用 `decide`，产出的 `DecisionOutcome`（acted / reason / utilities / silence_utility / advantage / hazard / action_probability / chosen_candidate_id）完整落进 `EndogenousOutcome.decision` |
| §45 | U_i = V_internal + V_user + V_relation − C_boundary − C_interrupt − C_repeat − C_risk | `motivation.py::candidate_utility`(150-266)、`typing.py::UtilityBreakdown` | 部分实现 | 七项全部落地且符号正确（internal 203-207、user 212-214、relation 215-219、boundary 221-223、interrupt 224-226、repeat 227-229、risk 230）；**差异**：代码另加两项——`− uncertainty_penalty·U`（`motivation.py:240`，`config.py:194` 默认 0.12）与缺席超过 48h 时的平坦 `+0.05`（`motivation.py:242-244`），因此 U_i 与文档式子并不相等（测试 `test_utility_components_follow_the_documented_formula` 断言的也是含 uncertainty 项的代码版，而不是文档的七项式） |
| §45.1 | V_internal = w₁N + w₂O + w₃E（N 内部需要、O 未尽之事、E 情绪吻合） | `motivation.py:197-207` | 部分实现 | 三项权重在（0.45 / 0.35 / 0.20，整体乘 `internal_gain=0.65`），E 由 `_emotion_alignment` 真实计算（`runtime.py:2088-2097`）；**差异**：另有一个 `urgency` 项（`motivation.py:197-201`，`urgency_gain=0.70`，取 `max(O·1.25, N·0.8)`），它是「有具体理由才说话」的实际开关，文档式子没有它 |
| §46 | V_user = Σ_y P(y∣i) r(y)：用户模型给出正向 / 中性 / 负向概率与边界风险，再做期望 | `motivation.py:208-214`、`user_model.py::Prediction`(110-138) | 部分实现 | 实际形状是 `user_gain · P_R · (0.55·P₊ + 0.45·P_C)`（0.85/0.55/0.45，`config.py:188`）：`P_R` 乘条件概率这一点与文档 §24 的口径一致，`boundary_risk` 也在 cost 项里发挥作用；**缺失**：`Prediction` 只有 `positive_probability` 与 `continue_probability`，**没有中性概率、没有负向概率，也没有奖励向量 r(y)**，负向下行只通过 cost 项（boundary / interrupt / risk）与（未接线的）保守分位数间接进入 |
| §47 | 边界相关候选不看平均接受概率，用 Q₀.₀₅ 保守下界；数据不足时自动更保守 | `motivation.py:627-629` + `_conservative_reply`(701-725)、`config.py:198/208`、`user_model.py::conservative_bound`(548-567)、`api.py:803` | 部分实现 | 决策路径确实对高危候选换用保守下界，但它不是分位数：`_conservative_reply` 用启发式 `clamp(P_R − z·type_factor·(0.5+U))`，`z=0.12` **硬编码在函数签名默认值里**（`motivation.py:702`），`type_factor` 按类型取 1.0 / 1.4 / 2.0；文档写的 5% 下界只存在于 `UserModel.conservative_bound`（`conservative_z=1.645` ≈ 单侧 5%，`user_model.py:562`），而它**只被巡检接口 `/user-model/predict` 调用**（`api.py:803`），不在决策链上；`UtilityConfig.downside_quantile = 0.05`（`config.py:198`）**全仓无读者**。触发门槛也不是「边界相关候选」而是 `prediction.boundary_risk > 0.30`（`config.py:208`），另有 `motivation.py:210` 一个 0.25 的硬编码阈值（该分支恒真、不可达，见缺陷 D3） |
| §48 | U_∅ = B₀ + aR + bB + cC − dI − eP² | `motivation.py::silence_utility`(107-147)、`config.py::SilenceConfig`(156-180) | 部分实现 | B₀=0.28、a=0.45、b=0.30、c=0.25、e=1.20 都在位且被 `decide` 消费（`motivation.py:589-595`）；P² 的语义与文档一致（压力越高沉默越难受，测试 `test_silence_becomes_more_painful_as_pressure_rises`）。**两处形状差异**：(1) **I 项符号相反**——文档是 `−dI`，代码是 `+0.42·I`（`config.py:179`，动机写在 `config.py:166-172` 与 `motivation.py:117-125`：克制地忍住不说本身就是被建模的行为，改符号会让「只是孤独」就跨线，破坏场景测试）；(2) C 项不是线性冷却，而是 `cooldown_active ? 1.0 : clamp(1−h/24)·0.5`（`motivation.py:138`），无冷却时最多只给半份权重 |
| §48 | 保持沉默也是**正式行动** | `motivation.py::decide`(648-674)、`typing.py::DecisionOutcome.silence_utility`、`reproduce_advantage`(758-768)、`probability_of_silence`(771-773) | 已实现 | 沉默不是一个「什么都不做」的早退分支，而是与每个候选同尺度比较的量：`eligible` 要求 `total > silence`，`advantage = max U_i − U_∅`，沉默时返回的具体原因（`no_candidate_beats_silence` / `cooldown_active` / `blocked_by_boundary`）与下次唤醒时间都成立 |
| §49 | D(t) = max_i U_i(t) − U_∅(t) | `motivation.py:648-650`、`typing.py::DecisionOutcome.advantage`(703) | 已实现 | `best` 取**未被硬拦截**候选的最大效用，`advantage = best − silence`；全被拦时记为 `−99.0` 而不是 `−inf`（`motivation.py:657`，测试 `test_advantage_never_reported_as_infinity`），并原样出现在 `DecisionOutcome.to_dict` |
| §50 | λ(t) = λ₀·ln(1+e^{βD(t)}) | `motivation.py::hazard_rate`(269-280) | 已实现 | `hazard_base * softplus(hazard_beta * advantage, beta=1.0)` 就是 λ₀·ln(1+e^{βD})（λ₀=3.0e-5 /s，β=4.0，`config.py:202-203`，文档未给数值）；D=0 时 λ=λ₀·ln2>0，与文档「低优势时仍有少量随机性」一致 |
| §50 | Δt 内 P(主动) = 1 − e^{−λ(t)Δt} | `motivation.py:676-687`、`runtime.py:1100-1101 / 1177` | 已实现 | `delta_t` 取**真实流逝秒数**（`elapsed_seconds = delta_seconds(stamp, last_tick_at)`，`runtime.py:1100`），不是固定心跳窗；动作判定 `random() > 1−e^{−λΔt}` 抛出，`next_wake_at` 由 `_next_wake` 反推 |
| §50 | 四个优点：无 0.799/0.801 跳变、不依赖心跳频率、压力高自然上升、低优势仍有随机性 | `motivation.py` 模块 docstring(6-17)、`hazard_rate`/`action_probability`、测试 `test_hazard_grows_with_advantage_and_is_smooth`、`test_two_short_ticks_match_one_long_tick`、`test_action_probability_is_a_survival_curve` | 已实现 | 连续性由 softplus 保证（0.7999 与 0.8001 的 λ 相对差 < 1%）；频率无关性由「对真实 Δt 积分」保证（两次 600 s 的存活率乘积 = 一次 1200 s）；没有 `if D > 阈值` 这样的死阈值存在于任何生产路径 |
| §51 | 决定行动后 P(i∣行动) = e^{U_i/T} / Σ_j e^{U_j/T} | `motivation.py:689-698`、`utility.py::softmax`(71-92)、`config.py:201` | 已实现 | T=0.35（`temperature`），`softmax` 数值稳定；**差异**：归一化只在 `eligible`（未被拦且 U_i>U_∅）子集上做，文档的 Σ_j 覆盖全部候选——即被硬边界拦掉的候选不会分到任何概率质量，这与 §52 的精神一致 |
| §52 | 硬边界不参与博弈，`allow_proactive=false` 直接阻止相关候选，不能出现「压力 0.99 压过边界成本」 | `runtime.py:1148-1154`、`motivation.py:602-614 + 245-246`、`authorize.py:112-118`、`delivery.py:212-240` | 已实现 | 链路完整：边界裁决在效用比较**之前**取得（`runtime.py:1149`）→ 主动候选被标 `blocked` 且 `total = −inf`（`motivation.py:245-246`）→ 从 `eligible` 与 `best` 中彻底排除 → 即使压力满值也不参与比较；投递前 `authorize` 再判一次（`authorize.py:112`），投递端以 `is_proactive=True` 复核（`delivery.py:220`，HTTP 发送口同样 `api.py:564`）。`force_allow` 参数存在但**从未被 `decide` 读取**（缺陷 D1），所以「强制轮次也不会绕过」靠的是 `boundary_denied` 而不是它 |
| §53 | 边界不是一个 bool：临时时间边界 / 主题边界 / 永久边界 / 条件边界 | `typing.py::BoundaryType`(114-120)、`boundaries.py::BOUNDARY_PATTERNS`(56-151)、`detect_boundaries`(161-213) | 已实现 | 本行只判「四类是否被建模并检测」：四类都有规则（`temporal` 多条含中英、`permanent`（`以后/永远/再也…`）、`topic`（`别一直问这个` → `repeated_interrogation`、`先别说这个` → `topic_avoid`）、`conditional`（`想自己待着`）），同 scope 去重取最强（`boundaries.py:206-212`），时长按 `boundary_respect` 缩放（189-193）。主题边界的**执行**缺口不在本行判定，见下两行与缺陷 D6 |
| §53 | 字段：boundary_id / type / scope / allow_reply / allow_proactive / starts_at / expires_at / revocable_by / source_event_id | `typing.py::Boundary`(420-467)、`projections.py:524-560`、`db.py:216-228` | 部分实现 | 九个字段全部存在（外加 `revoked_at` / `note`），落库与序列化都在位，`is_active()` 同时看 `revoked_at` / `starts_at` / `expires_at`（`typing.py:436-451`）；**`revocable_by` 存而不读**：没有任何策略代码读它，永久边界的保护实际是 `boundaries.py:248` 里对 `"撤回"/"取消"` 的子串硬编码判断（缺陷 D7） |
| §53.1 | 用户主动来找只表示「当前可以回应」，不自动恢复今天的主动权限 | `boundaries.py` 模块 docstring(12-14)、`evaluate`(337-354)、`detect_revocation`(230-252)、`runtime.py:1035-1037` | 已实现 | `evaluate` 每次都从「允许」重新算，只有活跃边界能剥夺主动权；用户消息不会解除边界（`process_user_message` 明确不写 `allow_proactive`，`runtime.py:1035-1037`）；只有命中 `REVOCATION_PATTERNS` 的显式措辞才会撤销（`boundaries.py:153-158`），且永久边界要求出现「撤回 / 取消」字样（248-250） |

---

## 二、常量对照表

只列**不一致**的，以及一致但值得钉住的（文档把符号写在公式里、没有给数值的，一并标出实际默认值）。

### 2.1 不一致 / 形状不同

| 文档 | 文档值 / 形状 | 代码实际值 / 形状 | 位置 | 说明 |
|---|---|---|---|---|
| §47 保守分位数 | **Q₀.₀₅**（5% 下界） | 决策链用启发式 `z=0.12`（硬编码）；`downside_quantile=0.05` 无读者；真正的 5% 下界 `conservative_z=1.645` 只被巡检 API 用 | `motivation.py:702`、`config.py:198`、`user_model.py:562`、`api.py:803` | 本节最严重的「常量与设计不符」：设计写 0.05、字段也叫 0.05，实际决策读的是 0.12 的启发式宽度 |
| §48 冲动项 | `− d·I` | `+ 0.42·I`（`impulse_gain`） | `config.py:179`（动机见 166-172） | 符号相反，且有测试背书；本表按「代码是有意的」记录 |
| §48 冷却项 | `cC` 线性 | `cooldown_active ? 1.0 : clamp(1−h/24)·0.5` | `motivation.py:138` | 无冷却时最多只给半份权重，不是纯线性项 |
| §43 联系候选价值 | `σ(aI+bP−cR)`（无先验） | `σ(−0.25 + 2.6I + 1.4P − 0.9R)` | `config.py:280-283` | 多一个中性先验 b₀=−0.25 |
| §45 U_i | 七项和 | 七项 + `−0.12·U` + （缺席>48h 时）`+0.05` | `config.py:194`、`motivation.py:240/242-244` | `uncertainty_penalty` 在文档里没有对应项 |
| §45.1 V_internal | `w₁N+w₂O+w₃E` | 三者加权（0.45/0.35/0.20，×0.65）+ `urgency_gain·max(1.25O, 0.8N)` | `motivation.py:197-207`、`config.py:197` | urgency 是「有理由才说话」的实际开关 |
| §46 V_user | `Σ_y P(y∣i)r(y)` | `0.85·P_R·(0.55P₊ + 0.45P_C)` | `motivation.py:212-214` | 无 P(中性)、无 P(负向)、无 r(y) 向量 |
| §40.1 `goal` | 动机不同 → 效用完全不同 | `goal` 不进任何效用计算 | `motivation.py::candidate_utility` 全文 | 只影响渲染与行动尝试记录 |
| §53 `revocable_by` | 字段参与策略 | 只存不读 | `typing.py:431`、`projections.py:539/560` | 无任何策略代码消费 |
| §53 临时边界默认时长 | 文档未给数值（示例只列了 `starts_at` / `expires_at` 字段） | 代码内部不一致：模式里硬编码 24h（`BoundaryPattern.hours` 默认值），再乘 `0.85+0.3·boundary_respect`（默认 0.88 → ≈26.7h）；而 `BoundaryConfig.default_temporal_hours=24.0` **无读者** | `boundaries.py:43/189-193`、`config.py:215` | 想通过配置改「临时边界默认多久」是无效的：真正生效的是模式里的字面量 |
| §35 压力积分 | 连续 ODE | 单次 tick 的积分窗被截到 21600 s | `motivation.py:478`、`config.py:153` | 长睡/长缺席时不是精确解 |
| §33 不确定性 U | 只列为输入之一（未规定形状） | 0.35 或 0.15 两档（按 `effective_count<3`） | `runtime.py:650` | 代理实现，属于「文档没要求、代码自己定的形状」 |

### 2.2 文档未给数值、代码已定的（钉住现状）

| 文档符号 | 代码默认值 | 位置 |
|---|---|---|
| §34 τ_I / τ_R | 5400 s / 9000 s（90 min / 150 min） | `config.py:139-140` |
| §35 β | 4.0 | `config.py:141`（与 §50 的 `hazard_beta=4.0` 是**两个独立常量**，可各自漂移） |
| §35 κ₊ / κ₋ | 6.0e-5 / 4.0e-5（每秒） | `config.py:142-143` |
| §36 ρ_I / ρ_P / ρ_R | 0.55 / 0.70 / 0.06 | `config.py:144-146` |
| §36 冷却时长 | 2400 s | `config.py:147` |
| §43 a / b / c | 2.6 / 1.4 / 0.9（另 b₀=−0.25） | `config.py:280-283` |
| §45 内部收益 w₁/w₂/w₃ | 0.45 / 0.35 / 0.20（× `internal_gain` 0.65） | `motivation.py:203-207`、`config.py:187` |
| §45 各成本增益 | boundary 0.75 / interrupt 0.18 / repeat 0.45 / risk 0.10 | `config.py:190-193` |
| §45 未文档化的项 | `uncertainty_penalty` 0.12、`urgency_gain` 0.70 | `config.py:194/197` |
| §46 用户收益权重 | `user_gain` 0.85、`P₊` 0.55 / `P_C` 0.45 | `config.py:188`、`motivation.py:213` |
| §48 B₀/a/b/c/d/e | 0.28 / 0.45 / 0.30 / 0.25 / **0.42(正号)** / 1.20 | `config.py:175-180` |
| §50 λ₀ / β | 3.0e-5 /s、4.0 | `config.py:202-203` |
| §51 T | 0.35 | `config.py:201` |
| §32 初值 | I=0.05、R=0.50、P=0.0 | `typing.py:315-317`、`db.py:64-66` |
| §33 缺席饱和 | 36 h | `config.py:150` |
| §45 重复容忍 | 2 次 / 3600 s 窗 | `config.py:199-200` |
| §43/§42 池参数 | `max_active` 12、TTL 21600 s、刷新 900 s（空池 300 s） | `config.py:272-275` |
| §52/§53 每日预算 | `max_contacts_per_day` 12 | `config.py:148` |

---

## 三、必须补的（按对「内源主动」这一目标的影响排序）

| # | 缺口 | 为什么影响内源主动 | 最小下一步 |
|---|---|---|---|
| 1 | **内源能「想到」的事只有三种形状**（§37/§40.2/§41）：`generate` 只产 `follow_up` / `curious_question` / `contact`；`share` / `repair` / `reply` 在白名单里却没有生成器；`emotion:` / `situation:` 两个 source 前缀没有产出者；没有任何内部代码派发 `TaskKind.CANDIDATE_GEN` | 主动消息的**内容**永远只能围绕「未完成的事」和「想起的旧事」；补丁 §12 明确要求的「重估 → 修复冲动 → 修复型候选意图」在默认配置下不可能发生，所以「追夫火葬场」这条叙事线断在候选生成上 | 在 `candidate.generate()` 里补两条纯规则候选：(a) 存在 `reappraisal` 事件且未过期 → `type="repair"`、`sources=["emotion:<event_id>"]`；(b) 最高活跃情绪强度 > 阈值 → `type="share"`、`sources=["emotion:<eid>","situation:<sid>"]`。不需要 provider |
| 2 | **主题边界从不参与裁决**（§52/§53）：所有内部 `evaluate()` 调用都不传 `scope` | 「用户说过别一直问这个」这类边界现在只是给渲染器的文本约束，候选仍可照常赢下博弈并被 commit；§52 承诺的「直接阻止相关候选」对 topic 类不成立 | 让候选能表达主题：`MotivationInputs` 增加 `candidate_scopes: Mapping[str, str]`，由 `runtime._action_spec` 或 `CandidateIntent.target` 推导；`decide` 里对每个候选调一次 `boundaries.evaluate(..., scope=...)`，把返回的 `allow_proactive=False` 变成该候选的 `blocked` |
| 3 | **`invalidate_when` 靠一张 4 条硬编码关键词表**（§42） | 模型写的任何失效条件都永远不会命中，陈旧候选会一直活到 TTL（6h）甚至被 commit 成一条已经不合时宜的主动消息 | 把 `candidate._condition_keywords` 的查表换成已经在用的通用分词匹配：`motivation._condition_tokens`（CJK 二元组 + 拉丁词）直接复用，条件里至少一个 token 出现在局势文本里即算命中 |
| 4 | **§47 的保守下界没有接进决策** | 边界风险高的候选目前用的是 `z=0.12` 的固定启发式，数据不足时「自动更保守」这件事没有真正发生——这是主动消息唯一的下行保护机制 | 把 `user_model.conservative_bound` 的结果喂进博弈：`runtime.py:1161-1164` 组装 `predictions` 的同时填一份 `conservative_replies[candidate_id] = user_model.conservative_bound(prediction)`，`MotivationInputs` 加该字段，`decide` 用它替换 `_conservative_reply`；随后删掉 `downside_quantile` 或让它成为 `conservative_z` 的来源 |
| 5 | **`V_user` 没有负向项**（§46） | 主动消息「会不会把用户推远」只通过三个 cost 项间接体现，缺少显式的 `P(负向)`，所以同一个 `P_R` 下「高概率被回复但被反感」与「低概率被礼貌回复」区分不够 | 给 `Prediction` 加 `negative_probability`（可由 `1−P₊−P_C` 归一或独立头），在 `candidate_utility` 里加 `− w_neg · P_R · P_neg` 一项，并把权重放进 `UtilityConfig` |
| 6 | **`dormant` 生命周期不存在**（§41） | 候选只会「新建 → 生效 → 到期/退役」，没有「暂时沉下去、条件再满足时浮上来」；当前局势下不合适的候选仍占据 `max_active=12` 的名额并参与每一轮博弈 | 在 `_refresh_candidates` 里对「precondition 在当前局势文本下不成立」的候选置 `DORMANT`（`projections.set_status` 已支持），并在 `precondition_holds` 转为成立时恢复 `ACTIVE` |
| 7 | **§36 的状态跃迁落在 commit 而不是 send** | 一次 abort / expire 掉的主动尝试（例如渲染失败、用户抢先回复）也会重置 `last_contact_at` 与 40 分钟冷却：什么都没发出去，却让自己更久不说话——直接削弱内源主动的频率 | 把 `motivation.release_after_contact` 从 `_commit_attempt`（`runtime.py:1352`）移到 `reducer.mark_delivered`（已经在那里写 `last_contact_at`/`last_exchange_at`/预算，`reducer.py:1714-1717`）；若担心渲染期间状态不更新，可保留 impulse 释放、只把 `last_contact_at`+`cooldown_until` 挪到投递处 |
| 8 | **§38.1 缺「近期执行过的意图」** | 生成器看不到自己刚刚做过什么，只能靠 `repeat_cost` 事后惩罚，容易产出高度相似的连续主动 | `DeepRefreshRequest` 加 `recent_attempts` 字段（`projections.attempts.list_by_state` 取最近 N 条 `sent`/`committed`），在 `build_request` 里填充；规则路径可在 `candidate.generate` 里用同表做去重 |
| 9 | **两条 source 命名约定没有对齐**（§40.2，缺陷 D13） | 模型生成的候选（也就是唯一能带来「新内容」的那条路）要么因 `ungrounded_sources` 被整条丢弃，要么落地后丢溯源与 conversation 路由——即 §37/§38 那条链路目前基本不可用 | 在 grounding 前统一命名空间：让 `Runtime._is_resolvable` 先剥掉 `unfinished:` / `memory:` / `emotion:` / `situation:` 前缀再查，或在 `ground_suggestions` 里把 source 规范化成裸 id 并在候选落地时用 `candidate.py` 的前缀常量重新拼回去；同时把允许的形式写进 `DEEP_REFRESH_SYSTEM_PROMPT` |

> 说明：第 1 条若选择「派发 `CANDIDATE_GEN`」而不是补规则，需要同时配置 `semantic.provider`，那是补丁 §17 明确的可选项，**不建议**把它变成内源主动的前提——所以两条里优先做规则版。

---

## 四、可以永远不做的

| 项 | 为什么不必做 |
|---|---|
| 把 §48 的 `−dI` 改回负号 | 代码的 `+0.42I` 是有意的、有场景测试背书的（`config.py:166-172` 写明了理由：负号会让「只是孤独」跨过阈值）。正确动作是**改文档 §48**，把式子写成 `+fI` 并说明「克制本身就是被建模的行为」 |
| 精确复刻 §45 的七项等式 | 多出的 `−0.12U` 与 48h 后的 `+0.05` 都是有效校准项；把文档补一句「实现另有 uncertainty 项与长缺席奖励」即可，不必为了公式对齐而删 |
| 在 §51 里对全部候选（含被拦的）做 softmax | 现在的 eligible 归一更符合 §52；给被硬边界拦掉的候选分配概率没有意义 |
| 把 §33 的 U 换成连续不确定性 | 两档代理（0.35/0.15）在没有更多校准数据前不会更准；`effective_count` 本身已经是连续量，将来需要时替换一个表达式即可 |
| 让 `revocable_by` 真正生效 | 现在只有一种撤销方式（显式用户撤销），枚举化策略没有第二个取值；保留字段作为 schema 记录，或直接删除，都不影响行为 |
| 去掉 §35 的 6h 积分上限 | 它是防长睡积分伪影的保护，上限不影响正常的分钟/小时级 tick |
| 让规则路径产出 `REINTERPRET` 操作 | 「重新解释一个候选」本质是语义工作（`pool.py:138-155` 写成新的 `interpretations` 版本），规则路径没有可解释的内容，交给模型路径即可 |
| 把 `reply` 从候选类型白名单里删掉 | 它作为「由用户消息触发的行动」占位无害，且 `is_candidate_proactive` 已把它排除在主动类之外 |
| `TaskKind.PROACTIVE_DRAFT` 的补齐 | 属于 §62–§65 的协议范围，不在本区块；行动尝试已经通过 `OutboxKind.RENDER` 完成同一件事 |

---

## 五、审计中发现的实际缺陷（只报不改）

> 以下均为**读代码得到的事实**，本次审计未做任何修复。

| # | 缺陷 | 证据 | 影响 |
|---|---|---|---|
| D1 | `MotivationInputs.force_allow` 是死参数 | 定义在 `motivation.py:67`，`runtime.py:1178` 传入，`motivation.decide` 全文从不读它（只有 `motivation.py:598` 的注释提到）；`decide` 的硬门只用 `boundary_denied = not inputs.boundary_allow_proactive`（`motivation.py:602-603`） | 注释声称「强制轮次仍拒绝行动、并返回本应适用的效用」——行为碰巧正确（靠 `boundary_denied`），但参数本身无效果，读者会误以为存在第二条判定路径 |
| D2 | 三套「保守/分位数」机制并存，决策用的是最不严格的那套 | `motivation.py:701-725`（z=0.12 启发式，决策链）、`config.py:198`（`downside_quantile=0.05`，无读者）、`user_model.py:548-567`（z=1.645 真下界，仅 `api.py:803` 巡检使用） | 巡检接口报出的「保守回复概率」与实际决策使用的数字不是同一个，运维据此判断风险会被误导 |
| D3 | 两个保守风险阈值，其中 0.25 的分支不可达 | `motivation.py:628` 用 `config.utility.conservative_risk_threshold`（0.30）；`motivation.py:210` 又写 `if prediction.boundary_risk > 0.25 and conservative_reply is not None`——`conservative_reply` 只在 risk>0.30 时非空，故该条件恒真 | 0.25 是第二处未文档化、不可配置的阈值；将来若把配置阈值调到 0.25 以下，行为会静默改变 |
| D4 | `invalidate_when` 的自然语言条件基本失效 | `candidate.py:463-474` 的关键词表只有 4 条（「已经得知后续结果」「已经得知面试结果」「用户表示不想聊这个话题」「用户表示不想被打扰」），表外条件返回 `[]`，`invalidated_by_situation`(457-459) 因 `if keywords and ...` 直接跳过 | 模型生成的失效条件全部静默失效，陈旧候选留到 TTL；而 `motivation.precondition_holds` 已经实现了通用分词匹配，两处口径不一致 |
| D5 | 主动后状态跃迁发生在 commit，与 `action.py` 自己的「committed ≠ sent」原则冲突 | 调用点 `runtime.py:1352`（`_commit_attempt` 内）；`action.py:9-13` 明文写着 committed 不等于 sent；`release_after_contact` 会写 `last_contact_at` 与 `cooldown_until`（`motivation.py:513-516`） | abort/expire 掉的尝试也会重置「多久没联系」的时钟并静默 40 分钟；而每日预算是投递时才计（`reducer.py:1711-1717`），两个账本口径不一致 |
| D6 | topic 类边界永不参与主动裁决 | 全部内部调用都不传 `scope`：`runtime.py:630`、`792`、`798`、`1149`、`scheduler.py:99`；`evaluate` 的 `scope_match = boundary.scope in {"all_topics", scope or "all_topics"}`（`boundaries.py:343`），scope=None 时 `topic` 边界永不匹配 | `repeated_interrogation` / `topic_avoid` 两条规则检测出来的边界只出现在渲染约束里；§52 的「硬边界阻止相关候选」对它们不成立 |
| D7 | `revocable_by` 存而不读；永久边界的保护是子串硬编码 | 字段写入/序列化见 `projections.py:539/560`、`typing.py:463`；唯一相关逻辑是 `boundaries.py:248` 的 `"撤回" not in text and "取消" not in text` | 想通过 `revocable_by` 表达「这条边界只能由明确指令撤销」的调用方拿不到任何行为 |
| D8 | `DeepRefreshRequest.memories` 的 docstring 与实际内容不符 | `providers.py:143` 写 "Activated memories"，`deep_refresh.py:459` 传的是 `projections.memory.list_memories(limit=8)`，其排序是 `ORDER BY importance DESC`（`projections.py:758`）；激活池另有查询（`projections.py:834/858`，按 activation 排序） | 深层刷新看到的是「最重要的 8 条记忆」而不是「此刻被激活的记忆」，与设计 §38.1「只放当前真正相关的」不符 |
| D9 | 声明了却没有产出者的来源前缀；以及 `repair` 候选不算主动候选 | `EMOTION_SOURCE_PREFIX` / `SITUATION_SOURCE_PREFIX`（`candidate.py:45-46`）无产出者；`validate_candidate` 允许 `share`/`repair`/`reply`（`candidate.py:368-377`），但 `is_candidate_proactive`(396-402) 不把 `repair`/`reply` 算作主动 | 模型产出的 `repair` 候选在 `decide` 里**不会被硬边界拦**（`motivation.py:612`），可以赢下博弈并被 commit（行动尝试、冲动/压力释放、`last_contact_at` 重置）。投递时仍会被 `authorize` 拦下（`delivery.py:216-240`），所以不会真的发出——但已经产生了一次无法投递的提交 |
| D10 | 7 个配置字段全仓无读者 | `utility_epsilon`(`config.py:206`)、`unfinished_relevance_weight`(284)、`commit_grace_seconds`(310)、`max_committed_attempts`(313)、`default_temporal_hours`(215)、`scan_recent_events`(216)、`downside_quantile`(198)；`grep` 全仓只有定义处命中 | 调这些值不会有任何效果；其中 `unfinished_relevance_weight` 尤其容易误导（`candidate_utility` 里对应权重是硬编码的 0.35，`motivation.py:205`） |
| D11 | 工作局势在投递路径也被写，但只有 ingest 路径做候选失效检查 | 写：`reducer.py:1718-1727`（投递成功后写入「我主动联系了用户：…」的事实）；检查只在一处：`runtime.py:1023` → `_invalidate_candidates` | 「每次局势变化后检查 invalidate_when」（§42）只覆盖了用户消息这一半 |
| D12 | `DriveConfig.beta` 与 `UtilityConfig.hazard_beta` 是两个独立常量，当前同为 4.0 | `config.py:141`、`config.py:203` | 压力 softplus 与危险率 softplus 的陡度本可分开调，但同名同值容易被误认为同一个参数；只改一处会造成「压力动态与行动概率」不同步 |
| D13 | 两条 source 命名约定没有对齐，模型产出的候选会「要么被丢、要么丢溯源」 | 规则路径写带前缀的来源：`unfinished:<id>`（`candidate.py:135`）、`memory:<id>`（`candidate.py:162`）、`internal_approach_drive`（`candidate.py:240`），`runtime._event_ids_behind`(1236-1260) 正是按这些前缀解析的；但深层刷新的 grounding 闸门是 `_is_resolvable`(1651-1693)，它按 `identifier.split("_",1)[0]` 取前缀，只认 `evt` / `mem` / `cnd` / `mcd` 与裸的 unfinished / emotion id——`"memory:mem_x"` 解析出的前缀是 `"memory:mem"`，直接落到 `False`。`deep_refresh.ground_suggestions` 对 `candidate_intent` 这类 `KINDS_REQUIRING_SOURCES` 会把带前缀的 source 判为 `ungrounded_sources` 并**丢弃整个操作** | 若模型按设计 §40.2 与代码自身规则生成器的写法给 `"memory:mem_123"`，操作被静默丢弃；若模型给裸 `"mem_123"`，操作能落地，但 `_event_ids_behind` 会把它当成事件 id 去查（`events.get_many(["mem_123"])` 查不到），候选因此丢失溯源与 conversation 路由，退回「谁最后说话就发给谁」。测试只覆盖了 `evt_*` 这种来源（`test_deep_refresh.py` 的 grounding 用例全部用 `evt_1` / `evt_ghost`），所以这条不一致目前无测试保护 |

---

## 六、判定行数统计

| 判定 | 行数 |
|---|---|
| 已实现 | 24 |
| 部分实现 | 15 |
| 未实现 | 0 |
| 被补丁取代 | 0 |
| 文档·约定 | 0 |
| **合计** | **39** |

未实现为 0 的原因：§32–§53 里每一个具名公式、字段与状态都在生产代码里有落点，问题集中在**形状差异**（§45/§46/§47/§48/§43 的附加项与符号）与**链路不完整**（§37 生成器、§38 输入、§40.2 source 命名、§41 dormant、§42 失效检查、§53 revocable_by）。
