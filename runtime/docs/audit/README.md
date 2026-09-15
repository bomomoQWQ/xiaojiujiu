# 设计文档 → 实现：缺口审计索引

把**原始 97 节设计文档**与 **PATCH v0.2** 一起对照代码的审计结果。此前只有 `../PATCH_V0.2_MAPPING.md`
覆盖了补丁的 §0–§33，设计文档从未核对过——本目录补上那一半。

**本文是索引与合并清单；每一节的证据（`file.py::symbol:line`）在三个分册里。**

| 分册 | 覆盖 |
|---|---|
| `block-a-state-and-cognition.md` | §1–§31 状态与认知（事件日志 / 工作局势 / 情绪 / 记忆 / 用户模型） |
| `block-b-motivation-and-action.md` | §32–§53 动机与行动（接近动力学 / 候选意图 / 博弈效用 / 边界） |
| `block-c-time-protocol-ops.md` | §54–§97 时间·协议·运维（tick / 调度 / 协议层 / 不变量 / 表设计 / 部署） |

审计基线 `8564327`（此后工作区仍在改动，分册里记录了各文件 sha256；行号以审计时读到的内容为准）。
审计**只读代码**，没有修改任何源码或测试。

## 一、判定合计

| 区块 | 已实现 | 部分实现 | 未实现 | 被补丁取代 | 文档·约定 | 行数 |
|---|---:|---:|---:|---:|---:|---:|
| §1–§31 | 38 | 25 | 1 | 3 | 0 | 67 |
| §32–§53 | 24 | 15 | 0 | 0 | 0 | 39 |
| §54–§97 | 20 | 19 | 1 | 2 | 3 | 45 |
| **合计** | **82** | **59** | **2** | **5** | **3** | **151** |

"被补丁取代"指本地 2B 模型路线相关的要求（设计 §8/§11/§61/§74、§88 Level 1/2、§89 Phase 3）——
补丁已正式删除该路线，因此不计为缺失。

## 二、必须补的（跨三块合并、去重，按对"长期连续性"这一目标的影响排序）

> **2026-09-15 状态更新（0.3.2）**：每条前面标出当前状态与证据；**原始判定文字保持原样**，
> 便于对照"当初是怎么判的"。约定：**[已修]** = 有回归测试或变异证据；**[部分]** = 关键一半已做、
> 另一半有意留待（写明是什么）；**[在飞]** = 正在改；**[未修]** = 尚未动。

1. **[已修 0.3.0/0.3.1]** 记忆永远无法形成（默认部署）。
   证据：`Runtime.consolidate` + `endogenous_round` 分支 + CLI `consolidate`（0.3.0）；
   随后 0.3.1 又修掉"形成后 12 小时就再也检索不到"等 9 条——**八条是新的记忆质量仿真先抓到的**
   （`scripts/e2e_memory_simulation.py`，25 项检查 + 两种注错自证）。变异：摘掉轮次里的调用 → 6 条失败。
2. **[在飞，且真因与原文不同]** 9 个写库入口不推进时间（§86.4）。
   实测发现真正的问题不是"哪个入口该 tick"：`endogenous_round` 的 hazard 区间是
   `stamp - state.last_tick_at`，**任何推进时钟的入口都会吃掉角色的等待窗口**——A/B 实测
   （同一三天窗口）里先 `GET /schedule` 一次，`delta_t` 从 260000 s 变成 **0.002 s**，
   `action_probability` 0.999999 → ~0；而候选效用对比**逐字节相同**（1.32705 vs 沉默 0.908808）。
   一次只读轮询清零三天开口冲动。修法：hazard 区间改为"距上次**决策**的时间"（持久化在
   `RuntimeState.meta`），且**只读端点不推进世界**。写入口仍不 tick（claim/render/deliver 是
   outbox 生命周期的一步，中间积分时间会与自己的上报步骤抢跑——实测让韧性两条不变量变红）。
3. **[已修 0.3.2]** 用户模型缺少时间性（§28/§29）。
   `tick_drift(dt)`（按 `exp(-rate·dt)` 只衰减超出先验的精度）已由 `Runtime` 的 tick 调用；
   半衰期是新配置 `user_model.drift_half_life_hours`（168 h），**刻意不复用 `forgetting_rate`**
   （那是每条观察的比例，一个旋钮两种单位正是这条路径失踪的原因）。回复延迟基线（`log1p` 空间 EMA
   + EMA 方差，存 `params_json`，不改 schema）已接线，样本不足 3 条退回
   `default_reply_delay_seconds`——该字段此前无读者，现在有了。组合性实测：7×1 天 == 1×7 天
   （四条路径 `uncertainty=0.375267`）。
4. **[已修 0.3.2]** 空缺证据没有生产者（§22.3）。
   `lazy_tick` 的清扫 `_record_absent_replies` 对已投递、超 `silence_after_hours`（36 h）无观察的
   attempt 记 `replied=False` 弱证据并像回复一样消费它。**比原文更严重的一点**：此前那条 attempt
   永远停在 `sent`，而"有在途 attempt"会堵死该会话后续全部派发——一条没人回的消息 = 该会话永久静音。
   "用户说过自己忙"现在会真的软化这条证据（此前只有回复归属路径读 busy 标记）。变异：摘掉清扫 → 3 条失败。
5. **[部分]** 候选生成只产出三种形状（§37–§43）。
   `share`/`repair`/`reply` 已有规则产出（`share` 来自关于用户的激活记忆、`repair` 来自负向观察或
   已声明边界、`reply` 来自未被回答的用户提问），`emotion:`/`situation:` 也有了生产者，41 条测试。
   **未做**：任何内部派发 `TaskKind.CANDIDATE_GEN`（那是 provider 路径），**runtime 侧接线**
   （把 observations/emotions/situations/boundaries/recent_events 传给 `generate`）待落。
6. **[已修 0.3.2]** 话题级边界不参与决策门（§52）。
   `Boundary.subject` 在声明那一刻绑定"这个"（含 `ADDED_COLUMNS` 迁移），绑定**优先用事件身份**
   （未尽之事 `source_event_ids`）而非文本重叠——实测「我明天下午三点面试，结束了告诉你」与标题
   「等待面试结果」只共享 1 个 bigram；决策门在效用比较**之前**剔除违规候选并报出原因；
   绑定不出来时**不猜、不拦**。16 条测试；实测还纠正了我的第一版（`repeated_interrogation` 曾拦掉
   所有提问型候选 72 小时，"话题级"被做成"全面禁问"，韧性仿真立刻抓到）。
7. **[部分]** `invalidate_when` 是 4 条硬编码关键词表。
   已改为由来源派生（未尽之事了结/记忆归档或被取代/问题已被回答/边界已撤回/情绪已过去），
   未知条件不再静默失效（审计 D4）。**未做**：runtime 侧在文本匹配为 None 时问
   `invalidated_by_source_state` 的接线。
8. **[已修 0.3.2]** 保守分位数未接线（§47）。
   决策路径改用模型自己的下界 `conservative_bound(prediction, quantile=config.utility.downside_quantile)`，
   硬编码 `z=0.12` 与不可达的 `0.25` 分支（缺陷 D3）已删除；`downside_quantile` 现在有真实读者，
   改它会改变决策（0.01 → 风险候选输给沉默；0.50 → 行动）。冷启动不被按界定价
   （冷启动 `boundary_risk≈0.20 < conservative_risk_threshold=0.30`）。**父代理独立复核**：
   动机侧"复刻"的公式与模型自己的实现一致到 **2e-5**，且 q 越小界越低（方向正确）。
9. **[已修 0.3.2]** `V_user` 没有负向/中性结果项（§46）。
   新增 `user_outcome_probabilities`（`P_good/P_neutral/P_bad` 是"用户回复了"的精确划分）与
   `V_user = g_u·P_R·[0.55·P_good + 0.45·P_cont − c·(0.18·P_neutral + 1.10·P_bad)]`，
   `c = 1 - uncertainty`（新配置项待提升）；"一个坏落地比一次完美交流略重"是刻意的非对称。
   **未做**：奖励向量、`Prediction.negative_probability` 训练头、`UtilityBreakdown` 里的
   `neutral_cost`/`negative_cost` 槽位（现在它们藏在 `breakdown.user` 里，`to_dict()` 看不到）。
10. **[未修]** 重估不派生下游状态（§67）。`reappraisals` 仍只写不读；
    tautology 重写后的场景 5 测试**明确声明只保护"不可改写历史"这一半**，不声称派生。
11. **[未修]** 6/7 类后台任务没有派发方（§63/§65）。
12. **[已修 0.3.2]** PG 后端在 SQLite-only SQL 上不可用且开关无守卫。
    `supports_durability_commands`（默认 False，失败关闭）+ `require_durability` 在任何语句/stat/mkdir
    之前抛类型化 `DurabilityUnsupported`；`open_database` 在 PG 未承认缺口时启动即告警一次；
    写入冲突改由后端中立 `db_base.ConflictError`（SQLite 侧双继承，旧捕获可用）。
    **父代理复核补的陷阱**：PG 在约束冲突后中止整个事务，故 `process_user_message` 的并发恢复读
    改为 savepoint 内插入再回滚到它，两个后端都能接着读。34 条测试 + 11 个变异。
13. **[已修 0.3.2]** §77 表名对照缺失。`runtime/docs/DESIGN_TABLE_MAPPING.md`：18 张逐条对照
    （同名 14/改名 1/合并 2/有意不实现 1），反向另有 5 张；并纠正本文件"8 张文档未列"的笔误（实为 5 张，共 21 张）。
14. **[未修，清单已备]** 死代码与无消费者字段的清理。
    只读审计已交付完整清单：**15 个死旋钮**（含审计没点名的 `EmotionConfig.event_reactivity`——
    唯一读者是死函数；`RuntimeConfig.boundary` 整块不可达）、**5 个 `EventType` 孤儿**、
    **12 个零调用函数**、若干只写不读字段。**注意**：`GET /config` 会序列化所有字段，删字段是
    响应形状变更；`POST /events` 接受任意 event_type，所以"无内部生产者"≠"外部不可达"——
    协议可达的成员（`TOOL_RESULT`/`REAPPRAISAL`/`Priority`/`Actor.TOOL`）要先决定宿主侧问题再删。
15. **[已修 0.3.2]** 恒真测试。21 条重写为"行为被破坏就失败"的断言（含点名的 §87 场景 4/5），
    **29 个变异全部击杀**，且把旧版本从 HEAD 抽出来跑其中 12 个变异 → **旧版全部存活**（前后对照）。
    没有删除或削弱任何测试。**父代理独立复核**其中一条（关系情绪衰减调制，落在无人在改的
    `emotion.py` 上）：删掉 `decay_rate *= 1.0 - 0.35*stability_commitment` → 该测试立刻变红，回滚后恢复绿。


## 三、可以永远不做的（合并后）

- 本地模型路线的一切：§74（量化档位/上下文长度/单线程/nice/cgroup/swap）、§88 Level 1–2、
  §89 Phase 3、§61 的 2B 单 Worker 队列（"合并"这个想法除外）。
- **embedding / RAG sidecar 与持久化向量（§75）** —— 词法兜底是当前唯一路径，已在
  `runtime/README.md` 与 `PATCH_V0.2_MAPPING.md` 记为有意降级；补丁也只说"可选"。
- §91 未来关系模型（文档自己标为非 MVP 阻塞项）。
- Prometheus 指标、常驻巩固 worker、CPU 优先级——长期演进项。
- §90 明确禁止的东西（多 Agent 自我讨论、每轮大模型反思、一功能一模型、"爱情值"核心状态）。

## 四、跨块反复出现的缺陷主题（比单条更值得注意）

1. **写了没人读**：config 字段、EventType、状态列、阈值、枚举——审计合计 24+ 处。
   它让"看起来实现了"成为默认印象，是这份审计存在的最大理由。
2. **只写不读的状态**：`supersedes` / `superseded_by_hint`、`UnfinishedStatus.CANCELLED`、
   `dormant`、`reappraisals`、`proposed_by`。
3. **两套不一致的编码**：行为特征在"预测"与"观察"两条路径上编码不同（question 一个按类型、
   一个按问号），模型学的与模型用的是同一个词的不同含义。
4. **恒真测试**（上面第 15 条）。
5. **写入口不推进时间**（上面第 2 条）。
6. **PG 分支不完整却没有守卫**（上面第 12 条）。

## 五、怎么复核

```bash
cd runtime && python -m pytest                    # 883 passed / 14 skipped（无 DSN）
CR_TEST_PG_DSN=postgresql://… python -m pytest    # 897 passed（对真 PG）
```

每一条判定都能用分册里的 `file.py::symbol:line` 定位；若某条与当前代码不符，以代码为准并在
分册里改判定——审计的价值在于**可复核**，不在于结论好看。
