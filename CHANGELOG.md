# 更新日志

版本号同时出现在 `runtime/pyproject.toml`、`runtime/src/companion_runtime/__init__.py`
（`/health` 里的 `runtime_version`）与插件仓库的 `metadata.yaml`。

---

## 0.3.1 — 2026-09-15

0.3.0 把记忆模块**接上**了；这一版把它**修对**。做法是先写一个记忆质量仿真
（`scripts/e2e_memory_simulation.py`），把设计文档 §15/§16/§18/§19/§20 的承诺逐条变成检查，
再照着实测到的失败一条条修——下面每条都先有可复现的现象，才有改动。

### 修复：记忆模块（每条都是仿真先抓到、再修）

- **记忆形成后约 12 小时就掉出工作集，24 小时后连直接相关的问句也检索不到。**
  `activation_decay_rate = 1.5e-4/s`（半衰期 **1.28 小时**）加上"只有 `active` 才能被检索"，
  使"长期记忆"的实际有效期是半天：探测脚本里"我生日是什么时候来着"在一天后返回**空**。
  现在半衰期是 27.5 小时（一个不再被想起的记忆约 2 天后退出工作集），并且
  **淡出的记忆仍然可被线索召回**（`_retrievable`：`active` 或 `low_activation`；归档与取代才是彻底退出），
  召回会把它**重新激活**回工作集。`active → low_activation → archived` 三档这才真正连起来。
- **激活值是"累计召回次数"，不是"现在有多在意"。** `base + (1-base)*score` 让所有常被召回的记忆
  一起饱和到 1.0，池子因此不再排序：老记忆永远压过刚说的话。改为
  `activation = max(activation, clamp(score))`（最强的一次近期召回，随时间衰减）。
- **"只靠重要度得分"的记忆也会被塞进池子。** 每条记忆都有 `0.3×importance + recency`，
  于是重要的记忆无论当下相不相关都永远过门限、永远不衰减。新增判据 `hit.recalled`：
  必须与当下句子或某条工作局势**共享 ≥2 个 CJK bigram**（短句被整条命中也算），
  与去重/冲突判据同一把尺；情绪项明确**不**参与（它按重要度对所有记忆等比例生效）。
- **工作局势的线索项对所有记忆都饱和。** 它先是"lexical 的复制品"（§20 的审计缺口），
  改真之后又变成"所有局势条目的词袋"，于是任何记忆都能拿到 0.7。现在**逐条计分、按短边归一**，
  并且局势里的"用户说：…"不再自动等于"想起了它"。
- **状态与池子会互相矛盾。** 记忆被挤出池子（衰减到极小或被 top-N 截断）时行被删掉、
  状态却还是 `active`：运维面显示"可检索"，而它永远进不了工作集，提示词的持久事实来源还会继续注入它。
  现在**任何一次离开池子都同时改状态**。
- **提示词只读工作集，于是它随工作集一起空掉。** 现在 `【必要记忆】` 有四个来源，轮流占名额：
  ①当下线索召回的；②**最近 24 小时刚学到的**（工作集会饱和，刚说的话排第 8 名就永远进不了四行版面——
  黑盒仿真正是这样抓到的）；③工作集里最"在心上"的；④持久事实（稳定知识/偏好/关系经历）按重要度。
- **修正被当成同意。** 极性判据是一串否定词短语，`不太喜欢` 里没有 `不喜欢` 这个子串，
  于是被判成**正面**：用户说"其实我现在不太喜欢咖啡了，改喝茶"，旧记忆毫发无伤，
  角色同时相信"喜欢咖啡"和"不喜欢咖啡"。现在极性按结构判定（正面标记前 3 字内有否定词即翻转，
  另有撤回词表），并且**新说法只能重定义更早的说法**：同一批巩固按时间顺序落地，
  迟到的旧说法以"已被取代"的身份入库（§18：不删过去，而是重新定义过去与现在的关系，两个方向都记账）。
- **相似的话会被当成同一件事而合并掉。** 去重只看相似度，于是"我平时喜欢喝咖啡"会被合并进
  "其实我现在不太喜欢咖啡了"——修正被吸收进它要修正的那条。现在**极性相反的两句永不合并**。
- **疑问句被当成"关于用户的稳定知识"。** `我生日是什么时候来着` 命中"我生日"标记，
  被存成 stable_knowledge（角色把**自己的提问**当成关于用户的事实）。现在疑问句最多是 episodic。
  同时补齐稳定知识标记（`我生日`/`生日是`/`住在`/`老家`/`工作`…），`relationship` 类别也终于有了产出规则
  （§16 的第四类此前只存在于枚举和重要度表里）。
- **记忆里挑事来问，会问已经知道答案的问题。** 记忆活得久了，"从记忆生成好奇问题"就挑到
  "面试过了！谢谢你那天惦记我"，在用户报完结果之后又问面试结果（黑盒仿真抓到）。
  现在这条路径尊重 `unfinished.subject_guards()`：**未尽之事已经占用的主体，记忆不再生成第二个候选**，
  该问的那条由未尽之事自己问。
- **死代码清零**：`tick_activation`（与 `decay_pool` 并行的第二套实现）、`pool_times`（无调用方）
  按审计 §14 的建议删除，而不是继续标注"未接线"。

### 新增

- **记忆质量仿真** `scripts/e2e_memory_simulation.py`：真文件 SQLite(WAL)、出厂默认配置（无模型无密钥）、
  一个多星期的普通对话，25 项检查覆盖"什么值得记 / 四类记忆 / 修正 / 两天后还记不记得 /
  淡出后还找不找得回 / 刚学到的在不在提示词里 / 遗忘不删除 / 没有凭空捏造的记忆"。
  只读两个面：运维面（`GET /memories`）与**提示词块**（宿主实际拿到的那段）。
  两处注错自证会咬：`--fault trivia`（门限归零，什么都记）24/25、`--fault no_maintenance`
  （维护永不运行）5/25。
- **`memory.fresh_window_hours`**：刚形成的记忆保证出现在提示词里的时长（默认 24 小时）。

### 验证（0.3.1 定版时点，本机实测）

| 套件 | 0.3.0 | 0.3.1 |
|---|---|---|
| `runtime` 离线测试 | 903 passed / 14 skipped | **914 passed / 14 skipped**（928 项） |
| `scripts/e2e_resilience_simulation.py` | 335 / 335 | **335 / 335** |
| `scripts/blackbox_user_simulation.py` | 77 / 77 | **77 / 77** |
| `scripts/e2e_memory_simulation.py` | —（本版新增） | **25 / 25**（+ 两种注错自证） |

### 已知边界（新增）

- 记忆的"以前…现在…"仍然是**结构化**的（旧的一条被撤回并记下替换者），**散文式改写需要模型**。
- 矛盾检测依赖极性标记与否定窗口；不带任何立场词的矛盾（"我戒咖啡了"式的间接说法）仍可能不被识别。
- 工作局势本身仍是"最近若干条用户消息"的廉价替身，审计 §20 那条"局势应表达当前状态"只做到
  "局势参与召回"这一半。

---

## 0.3.0 — 2026-09-15


这一版只做三件事：**把记忆模块真正接上**、**把存储从"只能 SQLite"变成可选 PostgreSQL**、
**把"复用宿主自带知识库"这条路验证清楚并写成契约**。三条不变量与 0.2.0 完全一致。

### 修复：记忆模块（审计 block A §13–§20、block C 必须补 #1）

改动集中在 `memory.py` / `runtime.py` / `scheduler.py` / `context.py` / `api.py` / `cli.py`，
回归测试在 `runtime/tests/test_memory_pipeline.py`（20 条，每条写的是它守的那个不变量）。

- **默认部署根本不会形成长期记忆**（最严重）。`consolidate()` 其实**不需要模型**，
  但它**没有任何调用者**：`semantic.provider="disabled"` 的标准部署里 `memories` 表永远是空的，
  `/memories` 永远空、激活池永远空、提示词里永远没有 `【必要记忆】`。
  现在 `Runtime.consolidate()` 是公开入口，`endogenous_round()` 在决策提交后按
  `needs_consolidation()` 触发一次（独立事务、独立失败域，失败只记录不炸轮次），
  `companion-runtime consolidate` 可手动跑一次；`EndogenousOutcome.consolidation` 永远存在，
  "没到点""跑完了""炸了"三者可区分。
- **候选的时间戳取的是墙钟**，于是重放/仿真时间线上"到点"永远不成立。改为由调用方传入
  本次 ingest 的时间（`propose_from_event(..., created_at=stamp)`）。
- **`scheduler._maintenance_due` 与真正干活的那条规则不一致**：它只看按价值排序的前 5 条，
  算出的唤醒点可能对不上实际会读的窗口（承诺一次什么都没干的唤醒）。现在两边都走
  `memory.next_consolidation_due()`。
- **去重判据写坏了**：`if not overlap: continue` 卡在 `ratio >= 0.85` 之前，使"几乎一字不差的重述"
  永远合并不了；同一段 `overlap` 还重复出现两次。现在顺序是：完全相同 → `>=0.85` 只按内容 →
  `>=0.6` 且至少共享一个主题 → 包含关系且共享主题。**被取代的记忆永不作为合并目标**
  （否则新说法会掉进一条再也检索不到的行里，无声消失）。
- **`supersedes` / `superseded_by_hint` 只写不读**：被新说法取代的事实仍然会被检索、进激活池、
  进提示词——角色会继续断言自己刚纠正过的版本。现在读侧统一走 `is_superseded()`
  （`retrieve` / `activated_memories` / `activation_strength` / `context.select_memories`），
  `/memories` 与 `state --include memories` 给出 `retrievable` / `retrieval_reason` /
  `superseded_by_hint`，并且 `low_activation` 的记忆不再从运维视图里消失。
  "以前…现在…"这种**散文式改写需要模型**，代码里如实写明只做了确定性的一半：被取代的事实不再被断言。
- **`MemoryStatus.LOW_ACTIVATION` 无处置位**：`active → low_activation → archived` 链条缺中间一环。
  现在 `decay_pool()` 降到 `activation_threshold` 以下即降级（只降 `active`，归档永不被复活），
  同一事实被重述时 `reinforce()` 加激活并提升回 `active`。
- **冲突检测用单字当"同一主体"**：读侧上线后，"不喜欢别人连续追问我在干嘛"会被
  "喜欢手冲咖啡"撤回（共用 我/喜/欢）。改用与去重同一个 CJK bigram 判据（≥2 个共享 bigram），
  `喜欢咖啡` / `不喜欢咖啡` 仍然判为冲突，无关的两句不再互相撤回；`superseded_at` 也改用本轮时间。
- 文档撒谎的地方逐条改掉（`build_situation` 承诺过 `active_items`、若干阈值配置名不存在、
  若干公式与实现不符等）；`tick_activation` / `pool_times` 确认无人调用，如实标注而不是假装修好。

### 新增

- **存储可选 PostgreSQL**（`runtime/src/companion_runtime/db_postgres.py`，可选依赖
  `pip install companion-runtime[postgres]`）：`DatabaseBase` 抽出事务模板（savepoint 栈、
  逐层 `BEGIN IMMEDIATE`），SQLite 与 PG 各自实现方言；`?`→`%s` 用 AST 级词法转换，
  `rowid`→`ctid`，`LIMIT -1` 重写，单写者用会话级 advisory lock。
  同一个测试套件在两种后端上都跑（`CR_TEST_PG_DSN` 打开 PG 专项）。
- **宿主知识库复用契约** `runtime/docs/HOST_KB_REUSE.md`：在真机 AstrBot 4.28.1 上实测出的四条事实
  ——插件 `initialize()` 早于 embedding provider 与 KB 初始化（必须用 `@filter.on_astrbot_loaded()`）；
  KB 必须绑定 embedding provider；**返回的 `score` 是"结果集内 min-max 归一化的融合分"而不是相似度**
  （无关问题也会给第一名 1.0，所以不能拿它卡阈值）；以及进程内/HTTP 两种调用形状。
- **用户黑盒仿真新增"记忆"阶段**（第 12 阶段，13 阶段共 77 项检查）：用户顺口说一次的事实，
  过一阵子必须真的被记住，并且**下次开口时被摆在模型面前**——读的是宿主实际构造的那次请求里的
  `【必要记忆】` 段（不是整块注入文本，否则"复读上一句"会被误判成"记住了"）。
  另加"每条记忆都必须追溯到用户自己打过的话"。`--fault memory` 让这三条必然失败，证明检查会咬。
- **三份审阅报告** `runtime/docs/audit/`（block A/B/C）与合并缺口清单。

### 黑盒仿真在这一轮抓到的缺陷

1. **"用户说完之后"的断言会随机误报**（断言的错，不是产品的错）：世界钟以 2 小时为一步，
   在用户开口**同一瞬间**、但**投递在其之前**的消息，会被时间戳窗口 `<=/>=` 误判成"之后"
   ——一次把"报结果后不再追问"打成失败，一次把"请求安静期间零主动消息"打成失败
   （改动前 7 次全量运行里误报 2 次）。transcript 是投递顺序（`Recorder.turns` 只追加、不排序），
   两次误报都证明那条消息排在用户发言**之前**。现在窗口归属同时看投递顺序与时钟
   （`Window.opened_after_turn` + `Story.in_window()`），`phase_boundary` / `phase_closure` /
   `phase_timeline` 三处判定统一。修好后连跑 3 次全量 77/77——产品行为本身一直是对的。

### 验证（0.3.0 定版时点，全部本机实测）

| 套件 | 结果 |
|---|---|
| `runtime` 离线测试 | **903 passed / 14 skipped** |
| 同上，接 `CR_TEST_PG_DSN` | 917 项（PG 专项 14 项不再跳过） |
| `scripts/e2e_resilience_simulation.py` | **335 / 335** |
| `scripts/blackbox_user_simulation.py` | **77 / 77**（断言修正后连跑 3 次一致） |
| 插件仓库 | 143 passed + 13 subtests |

变异验证（临时改回旧行为确认测试会失败，再改回）：把去重改回旧写法 → 重述合并那条失败；
把轮次里的 consolidate 调用删掉 → 恰好 6 条失败（`/memories` 空、激活池空、无 `【必要记忆】`）。

注错验证（每条都让对应检查失败、退码 1；每格跑两次，`topic` 因唤醒时刻受真实墙钟影响浮动 ±1）：
`leak` 3、`duplicate` 5、`topic` 10–11、`guilt` 9、`cross_session` 4、`default_session` 5、`memory` 3。

### 已知边界（新增）

- PG 后端**尚未在测试实例上接线**（`CR_STORAGE__DSN` + psycopg 镜像还没进 `docker-compose`）；
  `maintenance.py` 的备份/恢复/WAL 命令仍是 SQLite 专用，PG 后端需要方言闸门。
- 宿主知识库目前只验证到"可复用 + 契约清楚"，**记忆镜像到 KB、检索结果进提示词还没接**。
- 记忆的"以前…现在…"散文改写、以及不带极性词的矛盾检测，仍然需要模型或不支持。
- block A/B/C 审阅报告里的其余必修项（记忆/心跳时间推进只覆盖了部分写入口、用户模型时间动态、
  候选形状与 `CANDIDATE_GEN`、`invalidate_when` 硬编码表等）尚未处理，清单在
  `runtime/docs/audit/README.md`。

---

## 0.2.0 — 2026-09-15

第一个"可以拿去部署、也有人能接手"的版本。相对 0.1.0 的改动分四块：把审阅发现的缺陷逐条修掉、
把部署方式收敛到 Docker、把插件拆成独立仓库、补上一套用户视角的黑盒仿真。

### 修复（每条都有回归测试，测试文件名见括号）

**调度与生命周期**
- `serve` 现在真的把内源调度跑起来：`Scheduler` 在 `uvicorn.Server.serve()` 前后启动/停止，
  按运行时锚点自动调用 `endogenous_round()`。此前标准部署只监听 HTTP，角色永不主动醒来。
  （`test_runtime_lifecycle_fixes.py`）
- attempt/outbox 生命周期闭环：租约耗尽或终态拒绝会关闭 attempt 并取消兄弟行；孤立的
  待发 attempt 会过期；`sent` 永不被过期或重开。（同上）
- 用户回复只结算**最新一条已发出**的 attempt，且只结算一次；边界回复会记为负面证据。
  （同上）
- 当日接触数只在**投递成功**时计一次，且跨天先翻日再计。（同上）

**接口与并发**
- 渲染/投递上报改为单事务幂等：并发重复只应用一次，重复上报回答 `duplicate`。
  （`test_api_reliability.py`）
- `POST /rendered` 用 attempt 精确定位行（不再受 100 行分页影响），直接路径也一定排出发送，
  无法应用时给出显式 409 而不是静默丢弃。（同上）
- 重复 `event_id`（v0 与 v1）与重复 `task_id` 收敛而不是 500。（同上）
- 授权环节**传输中断**不再被当成业务拒绝：插件保持沉默、Runtime 用租约到期回收重投，
  显式的 `authorize_unavailable` 标记也走非终态重试分支。（同上 + 插件 `test_outbox.py`）
- 协议边界与时间入参加固：staleness 边界统一（gap 等于预算即过期，gap 0 永不 rebase），
  naive 时间戳在 v0 被 422 拒绝、在 v1 明确降级并记录。（`test_protocol_hardening.py`）

**认知正确性**
- 语义否定不再误判（`我没觉得我喜欢你` 保持未结算），`我先走了` 不再被当作死亡。
- 未尽之事按主体结算：`我到家了` 不会顺手关掉面试那件事，也不会重建刚解除的义务。
- 记忆去重按内容而不是来源；归档记忆不再被注入。
- 用户模型不确定度按每类证据数计算；解释缓存键包含主导情绪的方向与标签。
- `semantic.provider` 配置项真正生效；深层刷新的最小间隔改为持久化时间戳（重启不忘）。
  （`test_cognition_fixes.py`、`test_deep_refresh.py`）

**持久化**
- JSONL 事件镜像改为 **COMMIT 之后**才写：回滚不含镜像行，嵌套 savepoint 的提交顺序正确，
  镜像写失败不会让已提交的数据库状态失败。（`test_durability.py`）

### 新增

- **用户黑盒仿真** `scripts/blackbox_user_simulation.py`：真 uvicorn + 真文件 SQLite(WAL) +
  真插件钩子，12 个阶段 70 项检查，只依据**用户可见事实**（聊天记录、宿主回复、公开 HTTP 面）
  断言：不刷屏、边界即静默、不重复、不泄漏内部标记与凭据、会话隔离、重启与重放不打扰。
  自带 `--fault` 注错，用来证明每条检查真的会失败。
  0.2.0 时点：**70 / 70 全部通过**，
  复现：`python scripts/blackbox_user_simulation.py --base-dir <dir>`（退出码 0）。
- **高仿真故障恢复验证** `scripts/e2e_resilience_simulation.py`：335 项检查，覆盖并发上报、
  租约过期、断网恢复、重启续跑、队列 >150 行、多会话路由。
- **Docker 部署**：`Dockerfile`（非 root、状态全在 `/data` 卷、内置 healthcheck）与
  `docker-compose.yml`（runtime + astrbot 两个容器，端口只发布到 loopback）。
- **插件独立仓库**：<https://github.com/bomomoQWQ/astrbot_plugin_companion_runtime>
  （历史用 `git subtree split` 保留），主仓库不再包含插件代码。
- **`HANDOFF.md`**：换机器接手手册。

### 变更

- 许可证：MIT → **GPL-3.0-or-later**（`LICENSE`）。
- 项目定名 **小九九 / xiaojiujiu**（旧代号「理解痞老板」）。
- README 修掉了两条**本来就是错的**命令：CLI 没有 `--db` 选项（存储路径走 `--base-dir` /
  `--config` / `CR_STORAGE__DATABASE_PATH`，且 `--base-dir` 必须写在子命令前），
  `backup` / `verify` / `recover` 的参数形式也与文档不符。

### 黑盒仿真在 0.2.0 开发过程中抓到的缺陷

1. **报告结果的那句话重新打开了它刚关掉的义务**（已修复）：`面试过了！` 结算了
   `等待面试结果`，同一次 ingest 又因为 `result_reported` 规则不声明主体而重建了同一件事，
   于是机器人继续追问面试结果，连「以后别再提面试这件事了」都会把它再建一次。
   现在已了结的义务会在一个窗口内**保留其主体**（`unfinished.subject_guards()`），
   窗口内的任何提及都不算新承诺，窗口之后的新承诺仍然可以正常开新事。
   （`test_obligation_subject_and_routing.py`）
2. **在一个会话里形成的义务被投递到另一个会话**（已修复）：候选意图的来源是
   `unfinished:<id>` 而不是事件 id，路由把它当事件查、查不到，就退化成"谁最后说话就发给谁"。
   `Runtime._event_ids_behind()` 现在会把 `unfinished:` / `memory:` 解析回真正的事件来源。
   （`test_obligation_subject_and_routing.py`）
3. **跨会话回复归属**（已修复）：`_attribute_user_reply()` 用的是全局"最新一条已发出"，
   于是 A 会话里的一句话会结算掉发给 B 会话的主动消息——归属会消费 attempt，真实回复
   再也无法结算它，用户模型还会从"用户没看到过的消息"上学习。
   现在按回复到达的会话过滤（`_newest_sent_attempt(conversation_id=...)`）。
   （同上文件 `TestAReplyBelongsToItsOwnChat`，做过变异验证）
4. **仿真窗口假设**（非产品缺陷，已澄清）：`0.2.0` 过程中第 11 阶段曾报一条失败，
   原因是脚本给第二个会话只开了 48 小时窗口，而它承诺出的未尽之事能活 72 小时。
   已给"同一个仍未回答的承诺"补后续窗口，断言未被放宽。
   同一条提交里对"考试被发到群聊"的诊断是错的：发往群聊的每一行都属于群聊自己的未尽之事。
5. **主动消息的 prompt 不再提别的会话**（已修复）：主动消息的提示词会带上背景块，
   而背景块原本列出**所有**会话的未尽之事（既在未尽列表里，也在工作情境的"未尽之事：…"
   事实里），于是一个会话里的提醒可能被写成另一个会话的话题。
   现在按该消息要投递到的会话过滤（`api_v1._scope_matters`）；无法判定来源的未尽之事仍然保留
   （那是角色本来就知道的信息，丢掉反而是隐藏）。
   回归测试：`test_obligation_subject_and_routing.py::TestAProactivePromptStaysInItsChat`。

### 已知边界（未变）

`committed != sent` 之间、以及"平台已发出"与"结果已上报"之间的崩溃窗口仍然存在；
逾期未回复的 `sent` attempt 不做过期清理；授权断网走租约回收，长时间断网会消耗 attempt 预算。
完整清单见 README「已知边界」。

---

## 0.1.0 — 初始版本

Runtime sidecar 的第一版：情绪、记忆、用户模型、未尽之事、候选意图与动机决策，
AstrBot 薄插件（协议 v1），SQLite WAL 持久化，离线测试与基础真机验证脚本。
