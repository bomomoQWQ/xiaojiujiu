# 更新日志

版本号同时出现在 `runtime/pyproject.toml`、`runtime/src/companion_runtime/__init__.py`
（`/health` 里的 `runtime_version`）与插件仓库的 `metadata.yaml`。

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
  真插件钩子，12 个阶段 69 项检查，只依据**用户可见事实**（聊天记录、宿主回复、公开 HTTP 面）
  断言：不刷屏、边界即静默、不重复、不泄漏内部标记与凭据、会话隔离、重启与重放不打扰。
  自带 `--fault` 注错，用来证明每条检查真的会失败。
  0.2.0 时点：**69 / 69 全部通过**，
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
