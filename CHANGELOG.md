# 更新日志

版本号同时出现在 `runtime/pyproject.toml`、`runtime/src/companion_runtime/__init__.py`
（`/health` 里的 `runtime_version`）与插件仓库的 `metadata.yaml`。

---

## 0.3.2 — 2026-09-15（进行中）

按审计缺口清单继续修，顺序按"对用户行为的影响"排。本版目前完成三条。

### 修复

- **用户提问不再以提问句的形式进长期记忆，也不再挤占提示词名额（审计 #3 / #5）。**
  两条此前都不成立：8 条记忆的 summary 就是提问原句（"我喜欢你这件事情，你还记得我说过吗？"）；
  10 次探针里有 **4 次**记忆区被提问式记忆占满，而用户真正问到的那条披露**召回引擎确实返回了**
  （排名 4/10）却输给预算。
  现在的口径（用户确认）：**命题进记忆**——剥掉疑问框架，摘要读起来是一句事实；
  **疑问框架本身作为独立的关系证据保留**（用户在检查你是否记得，这本身就是关系信号），
  存在记忆的 `structured["recall_check"]`，**且不参与那 4 个名额**（仍可被线索召回，
  `MemoryStore.retrieve` 未改）。
  剥不干净时**原样保留**摘要，绝不产出半句话（"你还记得我跟你讲过它吗？"剥完为空，
  摘要就存原句）；但**框架照记**，所以它仍然不占名额——"探测到框架"与"剥出命题"是两件事。
  只有**疑问句**才认框架：含"记得"的陈述句（"我记得你说过喜欢我"）不受影响，否则会把
  一个真实披露误判成提问而藏出提示词。
  穿透四层：`MemoryCandidate` 新增 `structured` → `memory_candidates.structured_json`
  （含 `ADDED_COLUMNS` 与 `JSON_COLUMNS`，老库原地升级，旧行读回 `{}`）→
  `memory.consolidate` 合并（重复候选那条路也合并）→ `context.select_memories` 跳过。
  验收：`tests/test_question_memories.py` 三条（原 `xfail(strict=True)`，已转正并摘标记）；
  变异测试三处各自精确打红对应测试。详见 `HANDOFF.md` 的 #3+#5 一节，其中记了
  "这条验收测试原本不可证伪"的发现。

- **用户模型学到的行为特征，和它打分时用的特征，现在是同一个（设计 §22.1 / §25 / §27）。**
  `extract_features` 的 docstring 自己写着 `x = phi(A, C, Z)`（与 §25 一字不差），φ 只有一个、
  预测与观察都调它——不一致的是**交给它的 `A`**：预测侧给
  `emotional_expression`/`question`/`topic_shift`，观察侧一个都不给；两处观察点还用 **ASCII**
  `"?"` 判 `question`，而候选 intent 全是中文模板（`candidate.py` 的 `f"询问{matter.title}"`、
  `"没有具体事项，只是想和用户建立联系"`），所以那个特征在观察侧**恒为 0**。
  设计 §39 自己举的例子 `{"type": "follow_up", "intent": "询问用户今天的面试结果"}`
  就落在不一致里（预测 1.0 / 观察 0.0）。对着设计文档还查出三处内部矛盾：
  `TYPE_TO_BEHAVIOUR` 把 `share`/`emotional_expression` 映到同一行为类、把 `question` 映到
  `curious_question`，特征标志却只认 `share` 和另外三个类型；观察侧把 `proactive` 硬编码为
  `True`，而 `reply` 候选的 `is_candidate_proactive` 是 `False`；`POST /observations`
  又把调用方给的 `action` 原样透传，等于从前门再开一次同样的口子。
  现在 `user_model.describe_action` 是唯一的 `A` 构造器，运行时**五个**路径
  （预测、沉默清扫、投递回执、用户回复归属、无 attempt 的显式反应）全部走它；
  `describe_supplied_action` 给 API 入口规范化（`type`/`proactive` 是调用方对行为的描述，
  其余行为特征重算、伪造无效，未知键保留）；编码器里删掉了那个一直被 φ 静默丢弃的 `length`。
  验收：`tests/test_action_encoding_parity.py` 27 条（按类型断言**绝对**编码值，而不是
  "与预测一致"——后者在两边一起改错时仍会通过），`scripts/mutation_design_conformance.py`
  （仓库根目录，8 个变异）全部击杀。详见 `HANDOFF.md` 的"设计一致性清单"一节。

- **冷启动先验的注释不再自称中立，那句话变成了可执行的断言（设计 §31）。**
  `DEFAULT_THETA` 上方的注释原先写 "symmetric where the Runtime should stay agnostic"，
  而**每一列都是非零的**——乘在一个特征上的信念从来不可能是中性的，设计 §31 也只授权
  "低风险安全探索"、没说过先验应当对称。修法不是把先验清零（那会抹掉冷启动的探索倾向，
  是行为变更），而是让说法可执行：注释改为陈述真正的性质（**没有 suspicion ≠ 没有 opinion**；
  冷启动 `boundary_risk` 实测 0.175，远低于 `conservative_risk_threshold`；风险只因**已知边界**
  （+1.60）或**确证忙碌**（+0.30）上升），`tests/test_user_model_priors.py` 5 条把它变成断言：
  带意见的特征必须有写下来的理由（`DOCUMENTED_PRIORS` 与"哪些列非零"必须相等，新增或抹掉一条
  先验而不改理由就红）、冷启动不得把首次接触判成可能越界、边界与忙碌必须抬升风险、
  `explicit_permission` 必须是最强正向、`recent_contact_ratio` 必须比它更强地是负向。
  验收：5 条测试 + `priors` 组 7 个变异全部击杀。

- **撤回三条自己报错的缺陷（②③④）——记在这里以免再犯。**
  ② "深层刷新没有 tick 内调用方"：`runtime.py::endogenous_round` 第 1475 行本来就会跑一次
  触发判定，`tests/test_refresh_scheduling.py::TestRefreshRunsUnattended` 五条测试一直在。
  ③ "重解释不派生四类下游"：那四类下游就是 `reducer._apply_deep_refresh` 处理的六种 operation
  kind，本来就存在；只剩"`reappraisals` 没有读取方"并入死代码清理。
  ④ "§86.10 没有断言"：插件在 `mark_as_temp` 不可用时**拒绝注入**（失败关闭且只警告一次），
  `test_context_is_injected_as_a_temporary_part` 直接断言 `part._no_save`。
  三条都是**用自己编的词 grep、又直接信了审计文档的状态标签**造成的误判；教训写在
  `HANDOFF.md` 的"核实后撤回的三条"一节。

- **"被无视"这条证据终于有人产生（审计 #4 / 设计 §22.3）。**
  此前反馈回路只有**正面一半**：用户下次开口时，回复被归属到最新一条已发出的主动消息并结算；
  而一条**发出后石沉大海**的消息不留任何痕迹——`no_reply_weight` 那条路径在生产里**永远不可达**，
  于是用户模型只从"有回复"的样本里学习，无论用户怎么冷落它，它都学不到"这个人不太回我"。
  更糟的是那条消息的 attempt 永远停在 `sent`，而"有在途 attempt"会**堵死该会话后续的所有派发**：
  一条没人回的消息，等于把这个会话永久静音。
  现在 `lazy_tick` 里多了一道清扫（`Runtime._record_absent_replies`，与既有的"关掉投不出去的
  attempt"清扫并列）：已投递、超过 `user_model.silence_after_hours`（默认 36 小时）仍无观察记录的
  attempt，按 `BehaviourReaction(replied=False, reply_delay_seconds=<实际等待>)` 记一条观察，
  并像回复一样消费掉这条 attempt（结算 attempt、取消 outbox 行、关闭候选）。
  **它是弱证据，不是拒绝**：权重是 `no_reply_weight`（0.06）再乘 `1 - P(busy)`，
  "忙的时候没回"几乎不说明什么；`_target_rewards` 对非回复给的是 `0.5 × P(busy)` 这个**低目标**，
  而不是负反馈——这条从设计文档抄下来的语义没有改。
  `TickReport.absent_replies` 让这道清扫可观测。
  代价写在代码注释与配置注释里：attempt 在此被消费，所以**超过这个窗口才回的消息不再归属到那条消息**
  （算作主动联系）；等到天荒地老就等于永远学不到，二者只能选一个。
- **用户模型学会"随时间变虚"，也学会"对他而言算快还是慢"（审计 #3 / 设计 §28、§29）。**
  两处此前只有半边：
  ①**信念没有时间性**——漂移只在"有新观察落库"时跑一次，于是半个月不联系，模型对用户的把握
  与最后一天说话时**一模一样**；现在 `Runtime` 的 tick 会调用 `UserInteractionModel.tick_drift(dt)`，
  按 `exp(-rate·dt)` 只衰减"超出先验的那部分精度"（均值是历史事实、不动），
  效果表现为 `predict().uncertainty` 上升、`conservative_bound` 下降——也就是**久不联系就更保守**。
  半衰期是新配置项 `user_model.drift_half_life_hours`（默认 168 h），
  **刻意不复用 `forgetting_rate`**：那是"每条观察的nudge"，是个比例不是速率，
  一个旋钮两种单位正是这条时间路径当初失踪的原因（没有任何东西可读）。
  ②**回复延迟只有绝对值判据**——"这个人平时 8 小时才回、今天 2 小时就回了"学不到；
  现在模型持久化一条**该用户自己的延迟基线**（`log1p` 空间的 EMA + EMA 方差，存在既有的
  `params_json` 里，不改 schema），新延迟按对数空间的 z 分数打分；
  样本不足 3 条时不信任基线，退回 `default_reply_delay_seconds` 这个**此前没人读的配置**
  （现在它有读者了）。相对信号最多给 `positive_probability` 加 ±0.10，
  且**负向只能抵消已得的加分**：比平时慢只是"较弱的正面证据"，绝不是负面标签——
  与模块既有的"没有回复不等于负面"不变量一致。
- 组合性被验证：7 次一天的漂移与 1 次七天的漂移落在**完全相同**的位置
  （实测四条路径都是 `uncertainty=0.375267`），所以置信度不取决于时钟被轮询的频率。

### 验证

- 新增 `runtime/tests/test_absent_reply_evidence.py`（6 条）、
  `runtime/tests/test_user_model_time.py`（12 条，子代理，含 9 个变异各自的击杀证明）、
  `runtime/tests/test_user_model_time_wiring.py`（3 条，父代理补的**端到端接线**证据：
  单测只证明模型会老化，接线测试证明 Runtime 真的在 tick 里调它、持久化它能穿过 reload、
  且步进与一次性等价）。**既有测试文件一个没改**（`git diff --stat runtime/tests/` 为空）。
- 变异验证：摘掉 `_record_absent_replies` 调用 → 3 条失败；摘掉 `tick_drift` 调用 → 2 条接线测试失败；
  两次都恢复并复测。
- 全量：**983 项、0 失败、15 跳过 = 968 passed**；黑盒 **77/77**（连跑 2 次）、
  韧性 **335/335**、记忆质量 **25/25**。
- **PG 方言闸门与中立冲突异常（审计 #12）。** `maintenance` 的备份/恢复/检查点/WAL
  是 SQLite 专用机器，此前在 PG 上会半路炸成 `TypeError`/`OperationalError`，甚至可能
  看起来"做了一次备份"。现在 `DatabaseBase.supports_durability_commands`（默认 **False**，
  失败关闭）是闸门，`maintenance.require_durability(db, command)` 在任何语句、`stat`、`mkdir`
  或拷贝**之前**抛出类型化的 `DurabilityUnsupported(command, dialect)`；`as_database` 接受
  `DatabaseBase`；`open_database` 在 PG 且未显式承认缺口时**启动即告警一次**
  （`storage.durability_gap_acknowledged`），因为"PG 部署没有这些命令"是运维必须知道的事实。
  写入冲突改由后端中立的 `db_base.ConflictError` 表达（SQLite 侧是 `ConflictError` 与
  `sqlite3.IntegrityError` 的双继承，旧捕获继续可用），PG 侧在连接边界翻译。
  **父代理复核时补了一处它点到的陷阱**：PG 在约束冲突后会中止整个事务，所以
  `process_user_message` 里那次"并发写者抢先"的恢复读，现在把插入包在 **savepoint** 里再回滚到它，
  两个后端都能接着读。34 条测试；11 个变异各自击杀对应测试（闸门关闭 → 23 红，PG 能力位翻转 → 24 红…）。
- **§77 表名对照（审计 #13）**：`runtime/docs/DESIGN_TABLE_MAPPING.md`。设计文档 18 张建议表
  → 同名实现 14 / 改名 1（`emotion_events`→`active_emotion_events`）/ 合并 2
  （`user_model_global`+`contextual`→`user_model_params(scope)`）/ 有意不实现 1（`memory_embeddings`，
  依据补丁与 README 的既有降级记录）；反向另有 5 张实现有、设计未列的表。文档同时纠正了审计里
  "8 张文档未列"的笔误（自枚举只有 5 张，基线 `8564327` 与工作区都是 21 张）。
- **恒真测试（审计 #15）**：21 条断言重写为**会因行为被破坏而失败**的断言（含审计点名的
  §87 场景 4/5）。每条都有击杀变异；**29 个变异全部击杀**，并且把旧版本测试从 HEAD 抽出来
  跑同样 12 个变异 → **旧版全部存活**（前后对照，这是"以前确实恒真"的证据）。没有删除或削弱任何测试。
- **话题级边界进决策门（审计 #6 / 设计 §52）**。此前 `evaluate(scope=...)` 在决策路径里从不传 `scope`，
  而 `topic_avoid`/`repeated_interrogation` 两条规则的 `allow_proactive=True`——也就是说它们
  **永远走"允许"分支**，只在投递前才靠文案拦。根因是 `Boundary` 上**没有"这个"绑到哪里**：
  规则匹配的是指代性的「暂时不要跟我说**这个**」。现在：边界在声明那一刻绑定主体
  （`Boundary.subject`，含 `ADDED_COLUMNS` 迁移，旧库原地升级、旧行为 `NULL` 即"没绑定"），
  绑定**优先用事件身份**（未尽之事的 `source_event_ids`）而非文本重叠——实测「我明天下午三点面试，
  结束了告诉你」与标题「等待面试结果」只共享 1 个 bigram，纯文本匹配会失败；
  决策门在**效用比较之前**把违规候选剔除，并在 `decision["boundary_blocked"]` 里报出边界 id 与原因。
  **绑定不出来时不猜、不拦**（保守方向，投递前的文案闸门仍在）。16 条测试；
  实测还纠正了我自己的第一版：`repeated_interrogation` 曾拦掉**所有**提问型候选 72 小时，
  那是"话题级"被做成了"全面禁问"——韧性仿真立刻抓到（自主轮次不再排队渲染工作），
  现已按"绑定主体 + 提问形状"两个条件同时成立才拦。
- **入口推进时钟（审计 #2 / 设计 §86.4）——真正的问题不是"哪个入口该 tick"，而是 hazard 区间挂错了东西。**
  写入口（outbox 领取/确认、渲染上报、投递回执、proposal、观察、候选操作、未尽之事）此前不推进时间，
  于是紧随其后的 `/schedule`、`/authorize` 会基于滞后的 drive/hazard 判定。我先做了入口级推进点
  `Runtime.tick_for_entry`，但实测暴露出更严重的缺陷：
  **`endogenous_round` 的 hazard 区间是用 `stamp - state.last_tick_at` 算的**——也就是说，任何一次
  推进时钟的入口都会**吃掉角色的等待窗口**。实测 A/B（同一个三天的等待窗口）：
  先 `GET /schedule` 一次，`last_tick_at` 就被推到"现在"，下一轮 `delta_t` 从 260000 秒变成 **0.002 秒**，
  `action_probability` 从 0.999999 变成约 0；而候选的效用对比**逐字节相同**（最优 1.32705 vs 沉默 0.908808，
  优势 +0.418242）。**一次只读轮询把角色三天积累的开口冲动清零**，这比原审计抱怨的"决策基于滞后状态"
  严重得多。所以正确的修法是：把 hazard 积分区间改为"距离**上一次决策**的时间"（持久化在
  `RuntimeState.meta`，与既有的深层刷新节流同一手法），而不是"距离上一次时钟推进"；
  同时**只读端点不再推进世界**（`GET /schedule`、`/user-model/predict`）——查询不该改变角色行为。
  写入口仍然不 tick：claim/render/deliver 是 outbox 生命周期的一步，入口在中间积分时间等于与它正在
  上报的操作抢跑（把 tick 塞进 reducer 写入口时韧性仿真两条不变量变红，attempt 被从它自己的上报步骤
  底下老化掉）。
  实现过程中还踩到并修掉两个只在"挂住"时才现形的坑：**递归**（tick 自身要写库，写库又触发 tick）
  与**锁序反转死锁**（一个线程握着数据库连接要运行时写锁，另一个握着写锁要数据库连接）——后者在 DB 层
  加了**免锁**的事务深度镜像 `in_transaction_nowait`（`in_transaction()` 本身会在别人事务期间阻塞，
  用在守卫里必死）。不变量测试钉住：只读入口不改变下一轮的判定、两个守卫、递归、并发不死锁。

### 验证（0.3.2 定版时点，本机实测）

| 套件 | 0.3.1 | 0.3.2 |
|---|---|---|
| `runtime` 离线测试 | 914 passed / 14 skipped | **1044 passed / 15 skipped**（1059 项） |
| `scripts/blackbox_user_simulation.py` | 77 / 77 | **77 / 77** |
| `scripts/e2e_resilience_simulation.py` | 335 / 335 | **335 / 335** |
| `scripts/e2e_memory_simulation.py` | 25 / 25 | **25 / 25** |

另外这一版**由验证抓到并修掉的真缺陷**（不是我预先知道的）：

1. **`GET /schedule` 一次轮询清零三天开口冲动**（我引入的）：hazard 区间用
   `stamp - state.last_tick_at` 积分，任何推进时钟的入口都会吃掉等待窗口。A/B 实测：
   `delta_t` 260000 s → **0.002 s**、`action_probability` 0.999999 → ~0，而候选效用对比逐字节相同。
2. **`repeated_interrogation` 规则在中文里几乎不可达**：粒子分支 `(别|不要|不要再|不许)` 吃不下
   "**别再**追问我在干嘛"里的那个 `再`（只有"别一直问我这个"能命中），于是 §52 的这条话题边界
   从日常语言里根本声明不出来。我的 #6 测试直接构造 Boundary 对象，所以只测到"执行"、测不到"声明"。
3. **一条没人回的主动消息会把该会话永久静音**（审计 #4 的隐藏面）：attempt 永远停在 `sent`，
   而在途 attempt 会堵死该会话后续派发。
4. **`_record_absent_replies` 忽略"用户说过自己忙"**：回复归属路径读 busy 标记，沉默路径不读，
   同一句话对"回复"生效、对"沉默"不生效。

### 进行中

- **关系递进仿真**（`scripts/relationship_progression_simulation.py`，陌生人→熟人→朋友→恋人，
  含模拟时钟与后台 dump 的自动审视）：文件已写出并通过语法检查（8 阶段、公开 HTTP 面 dump、
  `--fault` 自证），**子代理仍在试运行与修 bug**，因此**未纳入本次提交**；它交付后单独提交。

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
