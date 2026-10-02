# 浪潮目的—指标审查（v1）

- 审查范围：`runtime/src/companion_runtime` 中浪潮收益编译、用户模型 v2 预测与训练、参数激活、readiness 决策、shadow/live、结算和运营证据。
- 方法：逐文件静态追踪定义、计算、持久化和消费；未修改运行代码，未提交。
- 配套登记：`runtime/audit/langchao_purpose_metric_registry_v1.yaml`。

## 1. 总结判断

当前系统具备较强的**审计结构**：四目标标签分别管理缺失/删失/歧义；预测带区间与支持状态；收益可复算；repeat 有软成本和硬上限；readiness 有逐步轨迹；shadow 有零副作用不变量；live 有 authority、allowlist、能力、边界和 durable snapshot 多重门禁。

但衡量能力仍有明确边界：

1. `reply / acceptance / continue / negative` 是固定窗口内的用户反应代理，不是长期满意度、关系质量或用户福祉。
2. 训练只持久化拟合与数值健康字段（objective、converged、sample_count、weight_sum、iterations 等），没有 Brier、log-loss、AUC、校准误差、验证集或长期结果评估。
3. 当前参数流程是“周期重训后 CAS 激活”，不是 champion/challenger 选模。
4. shadow `coverage/agreement` 只衡量机械可比性与一致性，不证明 baseline 正确，更不证明用户价值。
5. `attention` 当前固定全 1；`reply duration` 尚未作为指标实现；两者均不得在运营叙述中冒充已有智能信号。

因此当前可成立的窄结论是：**系统能可审计地生成短窗代理预测、保守编译候选收益并比较策略行为；不能据此声称浪潮优于 Runtime-v2，或模型已通过效果验证。**

## 2. 收益目标与代理

### 2.1 Reply

`Target.REPLY` 是曝光后固定窗口内是否出现可归因用户回复。标签只接受窗口内、用户来源、唯一 exposure 的事件；多 exposure 候选变为 `unattributable`。完整覆盖窗口到期且无回复时，才产生 `reply=false`（`user_model_v2_labels.py:162-184,207-250`）。浪潮 adapter 使用预测下界，模板基础金额为 `+1.0`（`langchao_runtime_adapter.py:46-55,319-339`）。

正当低值包括用户忙、消息无需回复、窗口较短、低压易忽略设计，或证据少导致区间宽。虚假高值可由碰撞错归因、窗口外回复、重复曝光、只展示 point 而忽略 interval 产生。Reply 不应成为“越高越好”的单一运营目标，否则可能奖励频繁或诱导性联系。

### 2.2 Continuation

Continuation 语义是 `P(continue | reply)`。无 reply 时不能把 continuation 当负例；只有 reply 已观察且窗口/覆盖完整，才允许 continuation=false（`user_model_v2_labels.py:257-299`）。v2 效用和浪潮 adapter 均将 continuation 条件概率再乘 reply 下界，避免把条件概率冒充无条件概率（`motivation_v2.py:208-215`；`langchao_runtime_adapter.py:324-329`）。

低 continuation 可以是一次性通知自然结束。应监控 `unknown/unattributable`，而不是将其偷偷转成零。

### 2.3 Explicit negative

Negative 只接受用户来源且 `explicit=true` 的明确负面事件；沉默、内部状态和边界存在本身不是负面标签（`user_model_v2_labels.py:219-228`）。完整窗口未观察到定义事件时可以 N=false，但源码明确说明这**不等于用户满意**（同文件 `1-10`）。v2 用预测上界无条件扣减；浪潮模板金额为 `-1.5`（`motivation_v2.py:199-215`；`langchao_runtime_adapter.py:46-55`）。

当前明确负面契约能减少心理揣测，但也会漏掉隐性不满；“negative 低”不能被包装为满意率。

### 2.4 Delivery、expression、rest

Delivery 是执行 witness，不是用户收益。adapter 给 delivery `base_amount=0`、预测概率 0；terminal ack 后才写 actual token（`langchao_runtime_adapter.py:340-352`；`langchao_live_repository.py:86-138`）。运营必须按 `accepted/delivered/read` 的 `delivery_basis` 分层（`user_model_v2_types.py:60-65`）。

`expression_delivered` 与 `rest_realized` 是本地动作完成 proxy，模板基础金额 `0.25`，可通过显式模板策略获得概率 1（`langchao_runtime_adapter.py:354-370`）。它们不能证明用户喜欢表达或从休息中获益。`DEFER_OR_REST` 命中后不发送（`langchao_live.py:95-108`），因此 rest/defer 率必须单独展示，防止“一致率很高”其实来自双方共同停摆。

## 3. Value profile、attention 与总收益

Runtime 八个价值轴一一映射到浪潮八方向，并归一化到固定总和 8；零和输入定义为全 1，缺轴、多轴、负数或非有限值拒绝（`langchao_runtime_adapter.py:242-274`）。这是人格治理参数，不是用户许可或效果 KPI。

收益公式为：

```text
expected_base = base_amount × probability
direction_total[d] += expected_base × outcome_direction_weight[d]
weighted[d] = value_weight[d] × attention_weight[d] × direction_total[d]
total_utility = Σ weighted[d] - Σ costs
attraction = total_utility / utility_scale
```

证据见 `langchao_reward.py:302-344`。未知 forecast 没有显式 template policy 时保持 `None`，不数值化（同文件 `223-229,302-310`）。硬边界明确不能编码成成本（同文件 `102-118`）。

Attention 的结构和版本均已进入 state/audit，但当前 adapter 固定 `all-one`（`langchao_runtime_adapter.py:277-279,486-495`）。所以当前 attention 不是有信息量的动态指标。`utility_scale` 由调用方传入，代码校验正数但未见集中审批/配置权登记；这是现有参数治理缺口。

## 4. Repeat cost

Repeat v2 只读取平台已确认的发送曝光，评估/渲染/候选生成不能增加历史（`repeat_v2.py:1-9,47-53`）。默认策略：

- 联系窗 6 小时，事项窗 24 小时；
- allowance 均为 1；
- 超额成本分别为 0.45、0.75；
- hard contact limit=4，hard same-matter limit=3。

详见 `repeat_v2.py:101-131`。用户对同一事项的新进展或明确 reopen 可重置同事项 run，但不清除全局联系负担（同文件 `183-276`）。成本进入 v2 net utility 和浪潮 `CandidateCostTerm`；hard reasons 直接 blocked/dropped（`runtime_v2.py:550-579`；`langchao_runtime_adapter.py:402-405,433-444`）。

运营应同时报告：recent contact count、same-matter count、两项软成本、total cost、hard-limit reason。只报 total 会掩盖归因错误。

## 5. Readiness、threshold 与决策动力学

Readiness 是候选随真实时间接近决定边界的状态，不是内容质量分。每个小步从同一个 before 快照计算所有候选，吸引力为正时提供增长，负吸引力、泄漏与竞争提供衰减；值被限制在 `[0,1]`（`langchao_engine.py:209-247`）。同 working set 才继承 readiness，工作集变化时新候选从 0 开始（`langchao_runtime_adapter.py:481-503`）。

`decision_threshold` 被约束在 `(0,1]`；首次越界立即停止，并列需要完整 tie order；预算耗尽可 defer（`langchao_engine.py:53-87,295-389`）。默认 wiring 为 `leak=0.2`、`competition_gain=0`、`threshold=0.99`、`time_scale=10s`、`max_step=0.5s`（`langchao_shadow_wiring.py:37-45`），即当前默认是“固定 attention、无候选竞争”的简化配方。因此：

- readiness 低可能是健康克制、新候选或竞争作用；默认配方下也可能因吸引力不足而长期不越 0.99；
- 降低 threshold 会机械提高/加快决定，不能解释为模型改进；
- dashboard 应按 parameter version 分层，展示 readiness 分布、首次越界时长、长期不越界率和 defer reason，而不是追求平均 readiness 高。

## 6. 训练、预测与“选模”

训练数据只取 active 且 observed positive/negative 的标签，并验证 scope、target、feature version/fingerprint 和 exposure 对齐；unknown、censored、unattributable、pending 均被排除（`user_model_v2_service.py:274-314`）。训练和预测共用冻结的 13 维特征编码，missing 有独立 mask（`user_model_v2_features.py:28-46,217-258`）。

每个 target 用 Gaussian prior 下的加权 logistic MAP，再形成 full-covariance Laplace 近似；时间权重按 observed time 至 fit time 的半衰期一次计算（`user_model_v2_estimator.py:82-109,112-222`）。预测提供 plug-in point 和参数可信区间，它不是包含结果噪声的预测区间（同文件 `225-254`）。

当前持久化训练诊断包括 `objective/support/sample_count/weight_sum/converged/iterations/training_exposure_ids` 等（`user_model_v2_service.py:336-356`）。未发现 held-out 评估、Brier、log-loss、AUC、ECE、校准曲线或候选模型比较。

`maintenance_v2` 默认每小时重训四头，完成后直接 `save_parameter_snapshot_and_activate`，以 expected snapshot 做 CAS（`maintenance_v2.py:90-110,201-262`）。因此：

> 当前没有“选模指标”；只有“最新拟合快照原子激活”。`fit_activated=true` 不能称为模型胜出。

若未来补选模，至少应把数据截止、验证集、目标级校准、覆盖、旧模型对照、晋级/回滚条件和批准主体写成独立契约。

## 7. Shadow agreement / coverage

Replay 以 Runtime-v2 decision audit 作为独立分母；只有存在稳定 `runtime-v2:<decision_id>` shadow witness 才算 covered，避免把已有 shadow 行作自身分母造成伪 100%（`langchao_shadow_replay.py:193-273`）。

- `coverage = covered_rows / candidate_rows`
- `agreement = agreements / covered_rows`
- agreement 同时要求 candidate 与 defer reason 一致；无 covered row 时为 `None`。

高 agreement 可能只是双方都 defer，或 coverage 很低；低 agreement 也可能是两种策略目标或时间动力学不同。因此它只能作为差异诊断，必须联报 coverage、unknown、defer、候选分布和参数版本，不能作为自动切 live 的门槛。

Shadow run 强制 `sent/reward/training/quota=0` 且 `outbox_id=null`，读取时还验证 audit hash（`langchao_shadow.py:165-228,258-323`）。这些是安全不变量，不是业务低绩效。

## 8. Reply duration

仓库中未找到 `reply_duration`、`reply_latency`、`time_to_reply` 等运行字段或消费者。现有 exposure `occurred_at` 与已归因 reply `observed_at` 足以离线派生：

```text
reply_duration = first_attributable_reply.observed_at - exposure.occurred_at
```

但必须遵守标签归因：只使用 user-origin、窗口内、唯一 exposure 的 reply；无回复记录 censored/无值，不能填 horizon；ambiguous/unknown/invalidated 不进入点估计。还应同时报 reply rate 与 censoring，否则只统计有回复样本会形成严重幸存者偏差。

结论：**reply duration 当前缺失，不能声称已监控；新增前也不应直接接入 reward 或 threshold。**

## 9. 参数权限与退出机制

权限不是一个总开关：

- 配置层：shadow 默认关；live 需 `live_enabled=true` 且 scope 精确进入 allowlist（`config.py:477-491`）。
- 持久权限层：authority revision + CAS pointer 决定 engine/mode/may_dispatch；只有 live engine 可 dispatch（`langchao_authority.py:16-31`）。
- live 执行层：必须 `langchao/live/true`、scope/working set/contract revision 匹配、当前边界通过、external capability 存在且 durable snapshot 可持久化（`langchao_live.py:75-200`）。
- 数值退出：首次 threshold crossing 决策；预算耗尽 defer。
- 候选退出：硬边界、hard repeat、blocked reason、重复语义候选被淘汰。
- 训练退出：零正权重退回 prior-only；优化失败抛错；feature fingerprint/matrix 不一致拒绝加载。
- shadow 退出：零副作用或 hash 不变量违反即报错。

代码层未见 RBAC。能改部署配置、调用 authority repository 或写数据库的运维主体实际上拥有参数/发布权；应在外部操作制度中明确双人审批、变更记录与回滚。

## 10. 运营可观测面的实际边界

浪潮目前主要留下 PostgreSQL state/step/shadow audit、hash 和离线 replay 报表；未发现专门面向 API/health/dashboard 的浪潮聚合指标端点。现有 replay 自动汇总的是 candidate/covered/coverage/agreement/defers/unknown 和候选分布，并证明 protected table delta 为零、authority 不变；它没有拆分 candidate agreement 与 defer agreement，也未直接产出 unknown rate、boundary leak、决定时长、僵持率或饱和时间。

另一个口径风险是：live 复用 shadow evaluation 阶段，`LangchaoShadowService` 开 round 时仍写 `run_mode="shadow"`（`langchao_shadow_service.py:252-280`），真正 live 身份由 authority、dispatch claim 与 live commit 表达。因此不能只按 round 的 `run_mode='live'` 统计 live 评估，否则会漏报。

## 11. 建议的解释纪律

1. 所有概率按 target、support、interval、horizon、delivery_basis 分层。
2. 所有收益按 reward/value/attention/template/parameter version 分层。
3. coverage 先于 agreement；coverage 不足时不下优劣结论。
4. rest/defer、hard-repeat、boundary block 必须作为首要运营结果，不得被“发送率”覆盖。
5. 训练健康与模型效果分开：objective/converged 不等于校准或泛化。
6. 在新增 validation 与晋级契约之前，称流程为“重拟合并激活”，不要称“自动选模”。
7. reply duration 若实现，必须联报 reply rate/censoring，且禁止单独优化。

## 12. 审计结论表

| 领域 | 现状 | 结论 |
|---|---|---|
| reply / continuation / negative | 独立标签、窗口、归因、区间预测 | 可作短窗代理；不可作长期满意度 |
| delivery | ack 绑定且本地结算 | 是执行 witness，不是用户收益 |
| expression / rest | 本地完成 proxy | 可定价动作完成；不可冒充用户结果 |
| value profile | 8 轴固定总尺度 | 已进入收益；属治理参数 |
| attention | 全方向固定 1 | 结构已接线，尚无动态信息 |
| repeat cost | 软成本 + 硬上限 | 已接入且语义清楚 |
| readiness / threshold | 可审计积分与首次越界 | 控制状态/参数，不是 KPI |
| training | MAP+Laplace，诊断充分 | 无泛化/校准评估 |
| model selection | 周期重训后 CAS 激活 | 当前不存在真正选模 |
| shadow coverage/agreement | 独立分母、只读比较 | 只能做机械差异诊断 |
| reply duration | 未实现 | 只能安全离线派生，不能宣称已监控 |
| live operations | 多重 deny-by-default | 权限边界较强，外部 RBAC 待治理 |
