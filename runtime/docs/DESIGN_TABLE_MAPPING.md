# §77「PostgreSQL 建议表」→ 实际 schema 对照表

> 本文是审计条目 13 的交付物：`docs/audit/README.md` 第二节第 13 条（§77 表名对照缺失）、
> `docs/audit/block-c-time-protocol-ops.md` §4.1 与 §6 第 8 条。
> 审计报告当时建议把这张对照放进 `db.py` 模块 docstring 或 `runtime/README.md`；
> 本次并行改动中这两个文件不归本文作者所有，因此独立成篇。
>
> **本文只做对照，不改任何代码。** 所有判定都能用下面的「复核命令」重新跑一遍。

---

## 0. 复核口径（先看这一节，再看表）

- **设计侧**：`内源主动型长期陪伴AI_Runtime_完整架构设计.md`（下文简称 `内源…md`）的
  `# 77. PostgreSQL 建议表`
  （第 3088 行起）。18 个名字来自第 3092–3111 行的 `text` 代码块，
  **逐字照抄，不做任何改写**；该节的正文只有一句「建议至少：」。
- **实现侧**：`runtime/src/companion_runtime/db.py::SCHEMA_STATEMENTS`（`db.py:36`）、
  `Database.ADDED_COLUMNS`（`db.py:607`）、`Database.migrate`（`db.py:619`）。
  全仓只有这一个文件执行 `CREATE TABLE`（`grep -r "CREATE TABLE"` 只命中 `db.py` 与
  `runtime/README.md` 的引用示例），PostgreSQL 后端逐字复用同一份 DDL：
  `db_postgres.py::PostgresDatabase.migrate` 执行 `db.SCHEMA_STATEMENTS`，
  `PostgresDatabase.ADDED_COLUMNS` 直接指向 SQLite 的那一份（`db_postgres.py:428`）。
  所以下面这张对照表对两个后端同时成立。
- **判定用词**：`同名实现` / `改名` / `合并` / `未实现` / `有意不实现`。
  - `未实现` = 仓库里没有任何表或列能承载该名字，且**没有**既有说明；
  - `有意不实现` = 仓库内已有文档把它记录为有意的取舍或降级。
- **不做「名字像就算实现」**：每一行都打开 DDL 与 `PRAGMA table_info`，
  在「依据」列写出**实际存在**的列（列名以 `Database(":memory:").migrate()` 后的实测为准），
  再给一个真实写入方符号。只写列名没有写入方的表会明确标注。
- **行号会漂移**：本文行号取自快照 `db.py sha256 7FE0CE5B9F53…`（2026-09-15 核对）。
  核对期间该文件的 `SCHEMA_STATEMENTS` 区被并行的另一处改动碰过一次
  （`boundaries` 增列 `subject`，见 `Database.ADDED_COLUMNS`）。因此
  **每一行的权威锚点是语句文本 `CREATE TABLE IF NOT EXISTS <表名>` 与符号名，行号只是方便定位**。
  其余被引文件的哈希见 §8。

---

## 1. 判定合计

| 判定 | 数量 | 涉及的名字 |
|---|---:|---|
| 同名实现 | 14 | `raw_events`、`runtime_state`、`working_situation_items`、`interpretation_versions`、`emotion_explanations`、`unfinished_matters`、`boundaries`、`memory_candidates`、`memories`、`activated_memories`、`interaction_observations`、`candidate_intents`、`action_attempts`、`background_tasks` |
| 改名 | 1 | `emotion_events` → `active_emotion_events` |
| 合并 | 2 | `user_model_global`、`user_model_contextual` → `user_model_params`（`scope` 主键） |
| 未实现 | 0 | — |
| 有意不实现 | 1 | `memory_embeddings` |
| **合计** | **18** | 对应实际 **16** 张表（两个名字并入一张表，一个名字没有表） |

---

## 2. 18 行对照

「创建位置」列里 `db.py::SCHEMA_STATEMENTS` 后面跟着的是该表的 DDL 语句文本（可 grep 定位）；
括号内行号是上面那个快照下的值。

| # | §77 原文名 | 判定 | 实现中的表名 | 创建位置 | 依据（已核对的列 + 真实写入方） |
|---:|---|---|---|---|---|
| 1 | `raw_events` | 同名实现 | `raw_events` | `db.py::SCHEMA_STATEMENTS`「`CREATE TABLE IF NOT EXISTS raw_events`」（`db.py:73`） | 实测 11 列：`event_id,seq,event_type,timestamp,actor,conversation_id,content,metadata_json,source_event_ids,runtime_version,created_at`；三个索引 `idx_raw_events_ts/_type/_conv`（`db.py:87-89`）；写入方 `eventlog.py::EventLog.append:83`（`INSERT INTO raw_events`，`eventlog.py:129`） |
| 2 | `runtime_state` | 同名实现 | `runtime_state` | 同上「`… runtime_state`」（`db.py:47`） | 实测 21 列，含 §79 要求的 `version`、`updated_at`，以及 §1 的时间锚点 `last_tick_at/last_user_message_at/last_contact_at/last_exchange_at`；`ADDED_COLUMNS` 给它补过 `epoch_at`、`last_exchange_at`（`db.py:608-609`）；写入方 `projections.py::RuntimeProjection.write:166`（另有 `ensure:94`、`bump_version:230`） |
| 3 | `working_situation_items` | 同名实现 | `working_situation_items` | 同上「`… working_situation_items`」（`db.py:250`） | 实测 11 列：`item_id,kind,content,confidence,salience,source_kind,source_id,created_at,updated_at,expires_at,status`；索引 `idx_wsi_status`；写入方 `projections.py::SituationProjection.upsert:257`。注：设计 §66.2 把同义表写作 `working_situation`（见 §5） |
| 4 | `interpretation_versions` | 同名实现 | `interpretation_versions` | 同上「`… interpretation_versions`」（`db.py:117`） | 实测 10 列，§79 的 `interpretation_version`、`supersedes_id`、`confidence`、`source_version`、`source_event_ids` 全在；索引 `idx_interp_target`；写入方 `projections.py::InterpretationProjection.add_version:1924` |
| 5 | `emotion_events` | **改名** | `active_emotion_events` | 同上「`… active_emotion_events`」（`db.py:191`） | 见 §3.1。实测 10 列 = §9.2 的 9 个字段（`emotion_event_id,source_event_id,direction,intensity,activation,target,semantic_label,created_at,decay_rate`）+ `status`；写入方 `projections.py::EmotionProjection.upsert:371` |
| 6 | `emotion_explanations` | 同名实现 | `emotion_explanations` | 同上「`… emotion_explanations`」（`db.py:205`） | 实测 6 列：`explanation_id,cache_key,payload_json,source,created_at,last_used_at`；索引 `idx_emotion_explanation_key`（`db.py:214`）；读写方 `projections.py::EmotionProjection.cached_explanation:414`、`store_explanation:442` |
| 7 | `unfinished_matters` | 同名实现 | `unfinished_matters` | 同上「`… unfinished_matters`」（`db.py:233`） | 实测 12 列，含 `status,waiting_until,mute_until,expire_at,resolution_conditions,resolution_note`；索引 `idx_unfinished_status`；写入方 `projections.py::UnfinishedProjection.upsert:601`（状态迁移 `set_status:631`） |
| 8 | `boundaries` | 同名实现 | `boundaries` | 同上「`… boundaries`」（`db.py:216`） | 实测 13 列：`boundary_id,type,scope,allow_reply,allow_proactive,starts_at,expires_at,revocable_by,source_event_id,revoked_at,note,subject,created_at`（`subject` 是核对期间由 `ADDED_COLUMNS` 新增的列）；写入方 `projections.py::BoundaryProjection.upsert:521`。注：设计 §66.2 写作 `active_boundaries`（见 §5） |
| 9 | `memory_candidates` | 同名实现 | `memory_candidates` | 同上「`… memory_candidates`」（`db.py:266`） | 实测 11 列，含 `value,status,consolidated_memory_id,topics_json,confidence`；写入方 `projections.py::MemoryProjection.upsert_candidate:708`（状态 `set_candidate_status:735`） |
| 10 | `memories` | 同名实现 | `memories` | 同上「`… memories`」（`db.py:281`） | 实测 12 列，含 §17「双表示」所需的两半：`structured_json`（结构化字段）与 `summary`（自然语言摘要），另有 `topics_json,importance,confidence,status,archived_at`；索引 `idx_memories_status`；写入方 `projections.py::MemoryProjection.upsert_memory:787` |
| 11 | `memory_embeddings` | **有意不实现** | 无 | 无 | 见 §3.2 |
| 12 | `activated_memories` | 同名实现 | `activated_memories` | 同上「`… activated_memories`」（`db.py:298`） | 实测 6 列：`memory_id,activation,last_recalled_at,recall_count,reason,updated_at`；写入方 `projections.py::MemoryProjection.upsert_activation:863`（读 `list_activated:830`） |
| 13 | `interaction_observations` | 同名实现 | `interaction_observations` | 同上「`… interaction_observations`」（`db.py:142`） | 实测 12 列，含 `attempt_id,action_json,context_json,outcome_json,attribution_confidence,source_weight,semantic_confidence,weight,applied`；写入方 `projections.py::UserModelProjection.record_observation:1760` |
| 14 | `user_model_global` | **合并** | `user_model_params`（`scope='global'`） | 同上「`… user_model_params`」（`db.py:308`） | 见 §3.3。实测 7 列：`scope,params_json,precision_json,observations,effective_count,last_updated_at,last_summary_json`；`scope` 是主键 |
| 15 | `user_model_contextual` | **合并** | `user_model_params`（`scope` 列可表达，但无生产者） | 同上「`… user_model_params`」（`db.py:308`） | 见 §3.4。表结构能表达 contextual，但全仓没有一个调用方写入非 `global` 的 scope |
| 16 | `candidate_intents` | 同名实现 | `candidate_intents` | 同上「`… candidate_intents`」（`db.py:319`） | 实测 19 列，含 §37 的 `sources_json,constraints_json,preconditions_json,invalidate_json,status,expires_at`；索引 `idx_candidate_status`；写入方 `projections.py::CandidateProjection.upsert:973`（状态 `set_status:1013`） |
| 17 | `action_attempts` | 同名实现 | `action_attempts` | 同上「`… action_attempts`」（`db.py:343`） | 实测 14 列，含 `state,based_on_version,committed_at,rendered_text,reconcile_action,superseded_json,outbox_id`；索引 `idx_attempt_state`；写入方 `projections.py::AttemptProjection.upsert:1115` |
| 18 | `background_tasks` | 同名实现 | `background_tasks` | 同上「`… background_tasks`」（`db.py:158`） | 实测 9 列：`task_id,task_type,priority,based_on_version,source_event_ids,status,created_at,settled_at,outcome`；写入方 `projections.py::TaskProjection.register:1693`（结算 `settle:1721`） |

---

## 3. 非「同名实现」的四行，逐条说清

### 3.1 `emotion_events` → `active_emotion_events`（改名）

- **不是缺表**：这是 18 个名字里唯一一个"设计自己就用了两个名字"的表。
  - §77 的清单写 `emotion_events`（`内源…md:3097`）；
  - 同一份设计文档 §66.2「当前投影」清单写的是 **`active_emotion_events`**（`内源…md:2689`）。
- 实现选了后者：`db.py::SCHEMA_STATEMENTS`「`CREATE TABLE IF NOT EXISTS active_emotion_events`」，
  `db.py` 模块 docstring 也把它列在"current projection"一类里（`db.py:9-12`）。
- 语义可核：字段与 §9.2 的九个字段逐一对上（`内源…md` §9.2 的 JSON 示例，第 748–756 行：
  `emotion_event_id`/`source_event_id`/`direction`/`intensity`/`activation`/`target`/`semantic_label`/`created_at`/`decay_rate`），
  多出的 `status` 承载"是否已衰减退场"
  （`projections.py::EmotionProjection.deactivate:402` 把它置为 `decayed`）。
- **判定理由**：相对 §77 的名字是改名；相对 §66.2 的名字是同名。这不涉及任何功能缺失。

### 3.2 `memory_embeddings`（有意不实现）

**仓库里已有六条既有说明（下表），且互相一致——所以判定是「有意不实现」，不是「未实现」。**

| 既有说明 | 出处（可核） |
|---|---|
| 「**未实现（有意）。** `memory.py::MemoryStore.retrieve()` 是词面重合 + 结构化加权，接口是留给 embedding sidecar 的接缝。补丁 §29 提到的"可选轻量 embedding"不存在；语义相近但用词不同的记忆检索不到。**这是记录在案的降级，不是缺陷**」 | `docs/PATCH_V0.2_MAPPING.md` 第二节 #7（第 93 行） |
| 「仍然未实现的三条都是**有意的既有边界**……embedding 检索仍是词法降级」 | `docs/PATCH_V0.2_MAPPING.md` 第 197 行 |
| 根 `README.md` §11「已知边界」表第一行：「记忆检索是词法降级，无 embedding」（影响："语义相近但用词不同的记忆检索不到"；原文该行含表格分隔符，此处转写） | 仓库根 `README.md` 第 465 行 |
| 「检索不依赖 embedding；`MemoryStore.retrieve()` 就是留给 embedding sidecar 的接缝（embedding 未就绪时本方案即为降级路径）」；另见「可选轻量 embedding（未实现，见第 16 节）」 | `runtime/README.md:395`、`:1322`、`:1368` |
| 「embedding / RAG sidecar 与持久化向量（§75）—— 词法兜底是当前唯一路径，已在 `runtime/README.md` 与 `PATCH_V0.2_MAPPING.md` 记为有意降级」 | `docs/audit/README.md` 第三节「可以永远不做的」第 75-76 行 |
| 代码侧的自述接缝：retrieval 用 FTS-like 词法重合代替 embeddings，`MemoryStore.retrieve` 是留给 embedding sidecar 的接缝 | `memory.py` 模块 docstring 第 22-23 行；`memory.py::MemoryStore:1109`、`::retrieve:1150`（打分公式见 `retrieve` docstring，无向量项） |

- **没有对象可存**：设计 §75 说「长期 embedding 应持久化」（`内源…md:3053`），
  但认知 Runtime 里没有向量生产者——`runtime/src/` 下字符串 `embedding` 只出现在
  `memory.py` 的模块 docstring（第 22-23 行）；仓库里其余 embedding 代码要么属于已归档的
  本地模型训练（`archive/local_model_training/`），要么是宿主 AstrBot 知识库的配置记录
  （`runtime/docs/HOST_KB_REUSE.md`），两者都与 `memories` 表无关。
  `MemoryStore.retrieve:1150` 的排序分是
  `lexical + situation + unfinished + emotion + recency + 0.3*importance - recently_recalled_penalty`，
  全无向量项。因此这张表即使建出来也无行可写。
- **一句话**：`memory_embeddings` 的缺失是设计 §75 整条路线未启用的**结果**，
  不是 §77 清单的一次遗漏。

### 3.3 `user_model_global`（合并进 `user_model_params(scope)`）

- 设计 §77 要求两张表；实现是**一张表 + `scope` 主键**：
  `db.py::SCHEMA_STATEMENTS`「`CREATE TABLE IF NOT EXISTS user_model_params`」（`db.py:308`），
  列 `scope, params_json, precision_json, observations, effective_count, last_updated_at, last_summary_json`。
- `global` 这一半是**真实存在且有写入方**的：
  - `projections.py::UserModelProjection.GLOBAL_SCOPE:1758`（值就是 `"global"`）；
  - `upsert_params:1854` 的签名是 `scope: str = GLOBAL_SCOPE`；
  - 生产调用点只有两个，都不传 scope：`user_model.py::UserModel._persist:561` → `upsert_params`（`user_model.py:573`）、
    `reducer.py:535` → `set_summary`（默认 scope）；
  - 读路径 `user_model.py:459` 调 `get_params()`（同样取默认 `global`），HTTP 侧 `api.py:803` 的
    `GET /user-model` 不接受 scope 参数。
- **设计这边本身也不一致**：§66.2 的"当前投影"清单里只有一张 `user_model_current`（`内源…md:2693`），
  并没有 global/contextual 之分。实现等于采纳了 §66.2 的"一张当前投影"，
  同时用 `scope` 主键保留了 §77 要求的可分层能力。**这是取舍，不是漏建表。**

### 3.4 `user_model_contextual`（同一个 `scope` 列，名存实无）

- 表结构能表达它：`user_model_params.scope` 是主键，`upsert_params`/`set_summary`/`get_params`
  都收 `scope` 参数，`upsert_params` 的 docstring 明说「Persist a parameter block (global or contextual)」
  （`projections.py:1865`）。
- **但全仓没有一个 contextual 写入方**：grep 整仓 `upsert_params|set_summary|list_params`，
  生产调用点只有 `user_model.py:573` 与 `reducer.py:535`（都不传 scope）；
  字符串 `contextual` 在 `runtime/src/` 里**只出现在上面那句 docstring**（`projections.py:1865`）。
  `list_params:1849` 会列出所有 scope，所以一旦有人写入就会出现——目前不会。
- **为什么没有写入方：无既有说明。**
  仓库内没有任何文档解释 contextual 半边为何不落数据；审计分册把它记为「**名存实无**」
  （`docs/audit/block-c-time-protocol-ops.md` §4.1 第 157 行），那是审计方的判定，不是实现方的理由。
  本文不替它编一个理由。

---

## 4. 反向：实际存在、但 §77 清单没列的表（5 张）

§77 的正文是「**建议至少**：」（`内源…md:3090`），所以它是一份**下限清单**，
"实现有、§77 没列"不构成偏离。但读这两份材料的人需要知道差额，否则会以为 schema 就是那 18 张。

| 实际表名 | 创建位置 | 设计文档里有对应吗 | 说明 + 写入方 |
|---|---|---|---|
| `schema_meta` | `db.py::SCHEMA_STATEMENTS`「`… schema_meta`」（`db.py:39`） | **没有**。全篇 grep `schema_meta` 无命中，PATCH v0.2 里也没有 | 只存一行 `schema_version`；写入方 `db.py::Database.migrate`（`INSERT INTO schema_meta(key,value,updated_at) … ON CONFLICT` 在 `db.py` 的 `migrate` 内） |
| `event_semantics` | 同上「`… event_semantics`」（`db.py:97`） | **没有这个名字**。它来自补丁 v0.2 的「`semantic_status = unresolved`」承载表，见 `docs/PATCH_V0.2_MAPPING.md:51`（补丁 §13 的映射行） | 14 列，含 `semantic_status,potential_relevance,settlement_source,deep_refresh_id`；索引 `idx_event_semantics_status`；读写方 `projections.py::SemanticProjection:2035`（`record_unresolved:2053`、`record_settlement:2082`、`settle_from_deep_refresh:2123`） |
| `reappraisals` | 同上「`… reappraisals`」（`db.py:132`） | **有，但不在 §77**：§66.1 的追加型清单叫 `reappraisal_events`（`内源…md:2679`），§67 正文叫 `reappraisal_event`（`内源…md:2722`） | 6 列：`reappraisal_id,source_event_ids,previous_interpretation,new_interpretation,delta_summary,created_at`；写入方 `projections.py::InterpretationProjection.add_reappraisal:1993`（读 `list_reappraisals:2022`） |
| `outbox` | 同上「`… outbox`」（`db.py:171`） | **没有**。设计文档全篇 grep `outbox` 无命中，补丁 v0.2 也没有 | 14 列，含 `kind,payload_json,status,priority,available_at,lease_owner,lease_expires_at,attempts,max_attempts,acked_at`；索引 `idx_outbox_ready`；写入方 `projections.py::OutboxProjection.enqueue:1220` |
| `attempt_events` | 同上「`… attempt_events`」（`db.py:362`） | **有，但不在 §77**：§66.1 叫 `action_attempt_events`（`内源…md:2678`） | 7 列：`attempt_event_id,attempt_id,from_state,to_state,reason,runtime_version,created_at`；索引 `idx_attempt_events`；写入方 `projections.py::AttemptProjection.record_transition:1147` |

小结：**21（实际）= 16（承载 §77 的 18 个名字）+ 5（§77 未列）**；
5 张反向表里，`reappraisals` 与 `attempt_events` 在设计 §66.1 有对应名字，
`schema_meta`、`event_semantics`、`outbox` 三张在设计文档里**完全没有出处**。

---

## 5. 设计文档自身的命名歧义（先说歧义，再谈判定）

§77 的**编号不歧义**：`# 77.` 是唯一的一级标题（`内源…md:3088`），18 个名字可以逐字照抄。
歧义在**名字层面**——同一张表在不同节里有不同写法：

| §77 的写法 | 同一文档别处的写法 | 实现的落点 |
|---|---|---|
| `emotion_events`（:3097） | `active_emotion_events`（§66.2，:2689） | `active_emotion_events`（跟 §66.2） |
| `working_situation_items`（:3095） | `working_situation`（§66.2，:2688） | `working_situation_items`（跟 §77） |
| `boundaries`（:3100） | `active_boundaries`（§66.2，:2690） | `boundaries`（跟 §77） |
| `user_model_global` + `user_model_contextual`（:3106-3107） | `user_model_current`（§66.2，单张，:2693） | `user_model_params`（跟 §66.2 的"一张"，但名字不同） |
| `action_attempts`（:3109） | §66.1 只有 `action_attempt_events`（:2678） | 两张表都有：`action_attempts` + `attempt_events` |
| §77 有 `memory_candidates`/`memories`/`memory_embeddings`/`background_tasks`/`emotion_explanations` | §66.1/§66.2 两份清单里**都没有**这五张（§77 的 `interaction_observations` 则在 §66.1 里，:2677） | 前两张同名实现，`memory_embeddings` 见 §3.2，后两张同名实现（§4 有反查） |

因此：任何"表名不一致"的统计都要先说清是拿 §77 比、还是拿 §66 比——
两份清单本身就不是同一份 schema。本文的 18 行只以 §77 为准。

---

## 6. 与审计分册的三处出入（结论一致，计数与行号有问题）

审计分册的**判定**与本文一致，但有三处需要更正，避免后来者按错的数字复核：

1. **「另有 8 张文档未列的表」是笔误，实际是 5 张。**
   `docs/audit/block-c-time-protocol-ops.md:76` 与 `:162` 都写「8 张」，
   但 `:162` 自己只枚举了 5 张（`schema_meta`、`event_semantics`、`reappraisals`、`outbox`、`attempt_events`）。
   本文用审计基线 `8564327` 复核过：`git show 8564327:runtime/src/companion_runtime/db.py`
   里的 `CREATE TABLE` 同样是这 21 张，所以在基线时也不可能是 8。
   **判为计数笔误（枚举是对的）**，不改判定。
2. **分册里的行号是审计基线行号，工作区已前移。**
   例：`block-c:157` 引的 `projections.py::UserModelProjection.GLOBAL_SCOPE:1753` 现为 `:1758`；
   `block-c:187` 引的 `db_postgres.py::PostgresDatabase.migrate:581` 现为 `:690`、
   `ADDED_COLUMNS` 由 `:329` 变 `:428`。§4.1 表里 `db.py` 的行号（39–361）
   在快照 `7FE0CE5B…` 下**基本仍成立**，但 `boundaries` 之后的行号已 +1（见 §0 的漂移说明）。
3. **「`README.md:434` 记为已知降级」这条落点已过期。**
   `block-c:74` 引的 `README.md:434` 在当前工作区不是那条内容；
   根 `README.md:465`（§11 已知边界表）与 `runtime/README.md:395`/`:1322`/`:1368` 仍然成立。
   **结论（embedding 降级是记录在案的）不受影响，只是引用要换。**

---

## 7. 复核命令与输出

### 7.1 主核对：列出 schema 真正创建的表

```powershell
cd runtime; $env:PYTHONPATH='src'; python -c @"
from companion_runtime.db import Database, SCHEMA_STATEMENTS, SCHEMA_VERSION
import re
names=[m.group(1) for s in SCHEMA_STATEMENTS for m in [re.search(r'CREATE TABLE IF NOT EXISTS (\w+)', s)] if m]
print('[A] CREATE TABLE statements in db.py::SCHEMA_STATEMENTS (DDL order)')
for i,n in enumerate(names,1): print(f'{i:2d}. {n}')
print(f'    statements={len(names)}  SCHEMA_VERSION={SCHEMA_VERSION}')
db=Database(':memory:'); db.migrate()
rows=[dict(r)['name'] for r in db.query(\"SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name\")]
print('[B] Real tables after Database(\":memory:\").migrate() (sqlite_master)')
for i,n in enumerate(rows,1): print(f'{i:2d}. {n}')
print(f'    live tables={len(rows)}  set(DDL)==set(sqlite_master): {set(names)==set(rows)}')
print('[C] Database.ADDED_COLUMNS (ALTER TABLE top-ups)')
for t,c,ty in Database.ADDED_COLUMNS: print(f'    {t}.{c} {ty}  present={c in db.column_names(t)}')
print('[D] any table whose name contains embedding:', any('embedding' in n for n in rows))
"@
```

输出（2026-09-15，`db.py sha256 7FE0CE5B9F53…`）：

```text
[A] CREATE TABLE statements in db.py::SCHEMA_STATEMENTS (DDL order)
 1. schema_meta
 2. runtime_state
 3. raw_events
 4. event_semantics
 5. interpretation_versions
 6. reappraisals
 7. interaction_observations
 8. background_tasks
 9. outbox
10. active_emotion_events
11. emotion_explanations
12. boundaries
13. unfinished_matters
14. working_situation_items
15. memory_candidates
16. memories
17. activated_memories
18. user_model_params
19. candidate_intents
20. action_attempts
21. attempt_events
    statements=21  SCHEMA_VERSION=2
[B] Real tables after Database(":memory:").migrate() (sqlite_master)
 1. action_attempts
 2. activated_memories
 3. active_emotion_events
 4. attempt_events
 5. background_tasks
 6. boundaries
 7. candidate_intents
 8. emotion_explanations
 9. event_semantics
10. interaction_observations
11. interpretation_versions
12. memories
13. memory_candidates
14. outbox
15. raw_events
16. reappraisals
17. runtime_state
18. schema_meta
19. unfinished_matters
20. user_model_params
21. working_situation_items
    live tables=21  set(DDL)==set(sqlite_master): True
[C] Database.ADDED_COLUMNS (ALTER TABLE top-ups)
    runtime_state.epoch_at TEXT  present=True
    runtime_state.last_exchange_at TEXT  present=True
    boundaries.subject TEXT  present=True
[D] any table whose name contains embedding: False
```

**怎么和 §2 的表手工对差**：把 [A]/[B] 的 21 个名字与 §2 的「实现中的表名」列相减，
差额应当正好是 §4 的 5 张反向表；`memory_embeddings` 在 [A]/[B]/[D] 三处都不出现。

### 7.2 列级核对（§2「依据」列的来源）

```powershell
cd runtime; $env:PYTHONPATH='src'; python -c @"
from companion_runtime.db import Database
db=Database(':memory:'); db.migrate()
for t in ('raw_events','runtime_state','working_situation_items','interpretation_versions','active_emotion_events','emotion_explanations','unfinished_matters','boundaries','memory_candidates','memories','activated_memories','interaction_observations','user_model_params','candidate_intents','action_attempts','background_tasks'):
    print(f'{t} ({len(db.column_names(t))}): ' + ','.join(sorted(db.column_names(t))))
"@
```

输出（同一快照，逐字粘贴）：

```text
raw_events (11): actor,content,conversation_id,created_at,event_id,event_type,metadata_json,runtime_version,seq,source_event_ids,timestamp
runtime_state (21): allow_proactive,approach_impulse,contact_count_today,contact_day,cooldown_until,epoch_at,foreground_pause_until,last_contact_at,last_exchange_at,last_tick_at,last_user_message_at,meta_json,mood_arousal,mood_stability,mood_valence,pressure,restraint,runtime_id,updated_at,values_json,version
working_situation_items (11): confidence,content,created_at,expires_at,item_id,kind,salience,source_id,source_kind,status,updated_at
interpretation_versions (10): confidence,content,created_at,interpretation_id,interpretation_version,source_event_ids,source_version,supersedes_id,target_id,target_kind
active_emotion_events (10): activation,created_at,decay_rate,direction,emotion_event_id,intensity,semantic_label,source_event_id,status,target
emotion_explanations (6): cache_key,created_at,explanation_id,last_used_at,payload_json,source
unfinished_matters (12): created_at,expire_at,mute_until,priority,resolution_conditions,resolution_note,source_event_ids,status,title,unfinished_id,updated_at,waiting_until
boundaries (13): allow_proactive,allow_reply,boundary_id,created_at,expires_at,note,revocable_by,revoked_at,scope,source_event_id,starts_at,subject,type
memory_candidates (11): candidate_id,confidence,consolidated_memory_id,created_at,kind,source_event_ids,status,summary,topics_json,updated_at,value
memories (12): archived_at,confidence,created_at,importance,kind,memory_id,source_event_ids,status,structured_json,summary,topics_json,updated_at
activated_memories (6): activation,last_recalled_at,memory_id,reason,recall_count,updated_at
interaction_observations (12): action_json,applied,attempt_id,attribution_confidence,context_json,created_at,observation_id,outcome_json,semantic_confidence,source_event_ids,source_weight,weight
user_model_params (7): effective_count,last_summary_json,last_updated_at,observations,params_json,precision_json,scope
candidate_intents (19): candidate_id,confidence,constraints_json,created_at,emotion_relevance,expires_at,goal,intent,internal_need,invalidate_json,preconditions_json,proposed_by,retired_reason,sources_json,status,target,type,unfinished_relevance,updated_at
action_attempts (14): attempt_id,based_on_version,candidate_id,committed_at,created_at,failure_reason,goal,intent,outbox_id,reconcile_action,rendered_text,state,superseded_json,updated_at
background_tasks (9): based_on_version,created_at,outcome,priority,settled_at,source_event_ids,status,task_id,task_type
```

### 7.3 项目测试

```powershell
cd runtime; python -m pytest -p no:randomly -q --no-header
```

本文是 docs-only 改动（全仓 grep `DESIGN_TABLE_MAPPING` 只命中本文自身，没有任何代码或测试引用它），
所以测试是否绿与本文无关。两点留档，方便下一个人复核：

1. **pytest 9 下这条命令看不到计数行。** 仓库 `pyproject.toml::[tool.pytest.ini_options].addopts`
   已含一个 `-q`，再传一个 `-q` 等于 `-qq`；pytest 9.0.2 在 `-qq` 下会把最后一行
   `N failed, M passed, K skipped` 一并省掉（只剩进度点、warnings summary 与 FAILED 列表）。
   要读计数就去掉多余的 `-q`：`python -m pytest -p no:randomly --no-header --tb=no`
   （基线口径 `983 passed / 15 skipped / 0 failed` 应当用这个读法复核）。
2. **核对当时工作区不是绿的，原因在并行的源码改动，不在本文。**
   - 本次核对**开始时**整套是绿的：`python -m pytest -p no:randomly -q --no-header` 退出码 0，
     无 F、进度行里恰好 15 个 `s`（与基线的 15 skipped 一致）；
   - 核对过程中复跑开始变红，且失败集合每次都在变（同一工作区先后读到
     `4 failed, 987 passed, 15 skipped` → `16 failed` → `307 failed, 684 passed, 15 skipped`，
     最后两次的 987/684 之和不同，说明并行改动正在中途重建测试集合）；
     之后的一次复跑更是**在 8% 处挂起超过 14 分钟无任何进展**（临时输出文件 844 秒未更新），
     只能终止——同一个工作区既能红也能挂。
   - 抓到的一条可复现栈与 schema 无关（行号为当时工作区版本，`runtime.py`/`reducer.py` 当时正被并行修改）：
     `runtime.py::Runtime.lazy_tick:532` → `_close_stalled_attempts:678` →
     `reducer.close_settled_outbox_attempts` → `reducer.py:87` 的写事务包装器 `self._tick(...)`
     → `runtime.py:428` 的 `tick=lambda now: self.lazy_tick(now)` → `RecursionError: maximum recursion depth exceeded`，
     当时受影响的是 `tests/test_refresh_scheduling.py` 与 `tests/test_motivation_bounds.py`。
   - 结论：**"测试保持绿"这一条无法由本文保证，也不该由本文承担**——本文只做表名对照，
     既不碰 `runtime.py`/`reducer.py`，也不碰任何测试（全仓 grep `DESIGN_TABLE_MAPPING` 只命中本文自身）。
     复核本文时请以 §7.1/§7.2 的两个脚本为准。

---

## 8. 引用状态：哪些引用没解决、哪些会漂移

**已解决（复核后换成了当前工作区可核的落点）**

- 审计分册 `block-c:74` 引的 `README.md:434` → 现落在根 `README.md:465` 与
  `runtime/README.md:395/:1322/:1368`（结论不变，见 §6.3）。

**无法解决的引用（如实记录，不猜）**

- 审计分册 `block-c:76`/`:162` 的「8 张文档未列的表」：**无法对应到任何真实集合**，
  枚举是 5 张，基线 `8564327` 与当前工作区都是 21 张表。判为笔误（§6.1）。
- 设计文档里**不存在** `schema_meta`、`event_semantics`、`outbox` 的出处：
  这是"确认不存在"，不是"没找到"（全篇 grep 零命中，补丁 v0.2 同样零命中，见 §4）。

**会漂移、复核时以符号为准的引用**

- 本文所有 `db.py` 行号：快照 `sha256 7FE0CE5B9F53…`。`SCHEMA_STATEMENTS` 区在核对期间被
  并行改动碰过一次（`boundaries.subject`），行号可能继续漂移；
  **请以 `CREATE TABLE IF NOT EXISTS <表名>` 的语句文本 + `db.py::SCHEMA_STATEMENTS` 为准。**
- 被引文件在快照时的 `sha256`（前 12 位）：
  `db.py 7FE0CE5B9F53`、`projections.py 7434C0B16BFE`、`eventlog.py 5CE065093626`、
  `memory.py 49CCD2AC1AC8`、`user_model.py FD6D6F1AB4D8`、`reducer.py 8F2CDA613D11`、
  `api.py 644C8732F896`、`db_postgres.py 5432932E6D13`、`maintenance.py 01F7BD32ABC2`。
  （`db.py`、`db_postgres.py` 在快照时是**未提交的工作区版本**，见 `git status`。）
- `runtime/README.md` §1 的目录树（第 57-58 行）目前只列了 `docs/PATCH_V0.2_MAPPING.md`，
  没有列 `docs/audit/`，也没有列本文。本文作者无权改 `README.md`，
  这里只作为**待接线项**记录：写完本文后，README 的目录树与第 47 行的"章节映射"指引
  仍然指不到 §77 的对照。
