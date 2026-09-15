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

1. **记忆永远无法形成（默认部署）** —— `memories` 的唯一写入者是 `memory.py::consolidate`，
   它的唯一调用者需要外部派发一条 `memory_summary` proposal；`needs_consolidation()` 没有任何
   调用方，CLI 没有巩固命令，插件不提交该 proposal。后果：`/memories` 恒空、激活池恒空、
   候选的 `memory:` 来源永不出现，README 描述的"后台巩固"在仓库内断链。
   最小下一步：`endogenous_round` 里加 `needs_consolidation → consolidate` 分支，
   或加 `companion-runtime consolidate` 并在 `serve` 的 maintenance loop 里按间隔调用。
2. **9 个写库入口不推进时间（§86.4）** —— outbox 领取/确认/退回、proposal、观察、候选操作、
   未尽之事、投递回执等入口都不 `lazy_tick`，于是紧随其后的 `/schedule` / `/authorize` /
   `/proposals` 会基于滞后的 drive 判定（时间不会丢，下一次 tick 会补算，但那一刻的决策是旧的）。
   最小下一步：把 tick 收进 `Reducer.process_proposal` / `mark_delivered` 的写事务开头，
   并把 `test_invariant_4` 参数化到每个入口。
3. **用户模型缺少时间性（§28/§29）** —— 漂移只在新观察落库时执行（与 Δt 无关，没有 tick 路径），
   且没有基线归一化："用户平常 8 小时回、今天 2 小时回"学不到；"很久没交互 → 旧认识变虚"不发生。
   这是唯一被标为**未实现**的一节。
4. **空缺证据没有生产者（§22.3）** —— 全仓库从不产生"主动后没回复"这类负向观察
   （`no_reply_weight` 那条路径实际永不触发），模型只从"有回复"的样本学习。
   最小下一步：对超时未回的 `sent` attempt 生成 `BehaviourReaction(replied=False)`。
5. **候选生成只产出三种形状（§37–§43）** —— `share` / `repair` / `reply` 在白名单里但从不生成，
   `emotion:` / `situation:` 来源前缀没有生产者，也没有任何内部派发 `TaskKind.CANDIDATE_GEN`，
   于是补丁 §12 的"重估 → 修复候选"路径不可能发生。
6. **话题级边界不参与决策门（§52）** —— 内部没有任何 `evaluate()` 调用传 `scope`，
   所以"硬边界挡住候选"对 `repeated_interrogation` / `topic_avoid` 不成立（只在投递前才拦）。
7. **`invalidate_when` 是 4 条硬编码关键词表** —— 模型自己写的失效条件永远不会触发。
8. **保守分位数未接线（§47）** —— 文档的 Q₀.₀₅ 存在（`downside_quantile = 0.05`）但没有读者，
   决策路径用的是硬编码启发式 `z=0.12`，真正的 5% 界只在巡检端点里被调用。
9. **`V_user` 没有负向/中性结果项（§46）**，也没有奖励向量。
10. **重估不派生下游状态（§67）** —— `reappraisals` 只写日志，"后来想明白 → 愧疚/修复"缺一环。
11. **6/7 类后台任务没有派发方（§63/§65）** —— 名义上存在的强 API 任务实际不动。
12. **PG 后端在 4 类 SQLite-only SQL 上不可用，且开关无守卫** —— `rowid` 排序、
    `LIMIT -1 OFFSET`、`maintenance` 的 PRAGMA 系列、`sqlite3.IntegrityError` 捕获。
    （其中 `rowid` 与 `LIMIT -1` 已在 `df3bd98` 修掉；`maintenance` 与异常类型仍待办。）
13. **§77 表名对照缺失** —— 18 张建议表 vs 实际 schema 有近名/缺表/合并（`memory_embeddings`
    未实现；`user_model_global/contextual` 合并为 `user_model_params(scope)`），没有对照文档。
14. **死代码与无消费者字段的清理** —— 合计约 24 个 config 字段、5 个 `EventType`、
    若干只写不读的字段没有任何读者；`emotion.py::appraise_event`、`should_re_explain`、
    `explain_and_store` 是无调用方死代码，建议删除而不是补接线（否则会给人"价值观敏感度
    与 busy 归因已在真实路径生效"的错觉）。
15. **恒真测试** —— §87 场景 4 的流程级测试是恒真断言（无主动消息、无观察、前后完全相同），
    场景 5 有一处 `hasattr(...) else True` 的恒真表达式；两处都给人一种"已覆盖"的错觉。

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
