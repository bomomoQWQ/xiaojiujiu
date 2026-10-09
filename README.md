# 小九九 · 跨时间连续的陪伴 AI Runtime

**让角色拥有持续的内在状态**——会记住、会重新理解、会形成想法，会在没有外部命令时
自己决定要不要联系你。它不是一个每轮重新表演人格的聊天 Prompt。

宿主 Bot 负责"现在这一刻怎么回应"，小九九负责"这一刻过去以后留下什么"以及"要不要开口"。
设计目标不是让它更会聊天，而是让它**记得住、沉得下、会自己想开口**，
并且**不会因此发疯**——不骚扰、不越界、不把沉默误读成恶意、不忘掉发生过的事。

> **它不是一个开箱即用的产品**：PostgreSQL、基础 compose 与库口令仍要你自己准备。但
> 「别人部署不了」已经不再成立——运维工具、逐条自检、版本化发布与部署手册都在
> （§6.10 逐条列了做了哪些、还差哪些；差的都是**向导**这类便利，不是能力）。
> 部署能照着 [`docs/部署与运维.md`](docs/部署与运维.md) 跑通，只是别期待点一下就好。

名字取自"心里的小九九"：它确实整天在打小九九——候选意图、效用打分、危险率、边界成本
排队算出"现在说这句话值不值"。

~~其实我之前想叫“理解痞老板”的~~

## 1. 它解决什么问题

普通聊天机器人的"人格"活在上下文窗口里。窗口一换，上一轮的情绪、承诺、没说完的事全部消失
——它永远只有"此刻"，没有"这几天"。这问题也没法靠把提示词写长解决：有长度上限，而且**不可审计**。
你想知道"它今天为什么突然不说话了"，只能把提示词读一遍然后猜。

```text
用户消息 ──► 宿主 Bot（AstrBot + 主 LLM）
                │  即时演出：看原话 + 上下文 + 人格，当场回应
                └──► Runtime sidecar（本仓库核心，每人一个进程）
                       持久认知：情绪余波 / 记忆 / 未尽之事 / 用户模型 /
                                 候选意图 / 主动动力 / 社会关系 / 「浪潮」
                       └──► 未来轮次：上下文注入 + 主动发消息
```

|          | 即时演出层                               | 持久认知层                               |
| -------- | ---------------------------------------- | ---------------------------------------- |
| 谁负责   | 宿主主 LLM                               | Runtime                                  |
| 回答什么 | "用户现在说了这句话，我这一刻怎么反应？" | "这件事结束以后，它在我身上留下了什么？" |
| 时间尺度 | 毫秒～秒                                 | 小时～天                                 |
| 依赖模型 | 是（主 LLM 本身）                        | **否**（确定性代码）               |
| 出错时   | 回复难看                                 | 记错 / 越界 / 骚扰                       |

---

## 2. 五条设计要点

1. **两个时间尺度。** 主 LLM 管"现在这一刻怎么反应"，Runtime 管"这一刻过去以后留下什么"。
   Runtime 不要求当前轮语义完备，只要求长期状态连续：听不懂可以先记下来，
   但不丢证据、不瞎猜。
2. **数值负责动力学，语言负责语义。** 状态内部是数值（冲动 / 节制 / 压力 / 心境 / 边界风险），
   但不直接喂给主 LLM——大模型会围着数字表演。中间隔一层解释器压成自然语言。
   反过来，叙事不能决定状态。
3. **事实、推断、解释分开。** 原始事件不可变；解释只能追加新版本并声明它取代了谁；
   推断必须能追溯回真实发生过的事。语义模糊的句子宁可不猜，标成未决等以后结算。
4. **决定"要不要开口"的是危险率，不是阈值。** 阈值有悬崖效应（0.799 vs 0.801），
   而且结果依赖心跳频率。危险率把"要不要开口"变成时间的连续函数，
   **心跳快慢不改变期望行为**（有专门测试守着）。沉默是一个正式候选，而且大多数时候它应该赢。
5. **不给每个认知问题都塞模型。** 默认部署不需要任何模型：语义端口是关的，
   系统靠规则、统计和状态机工作，关键路径毫秒级。可选端口是加法，不是前提。

> 分工一句话：**模型只负责语义，动力学交给代码，连续性交给数据库，"不发疯"交给协议层。**

---

## 3. 「浪潮」决策引擎

指导思想：**人是一切社会关系的总和**。

放进这个项目，意思是：角色该不该做什么，不由"我想说什么"决定，而由**此刻这段关系处在什么状态**决定。
候选从关系里长出来；做过之后的结果，又反过来改变关系。

### 3.1 怎么选

候选之间会互相竞争，不是各打各分再取最高分：

```text
τ_i · dx_i/dt = (1 − x_i)·[u_i]₊ − x_i·( ℓ_i + [−u_i]₊ + γ · Σ_j c_ij · x_j )
```

想做的那件事自己会涨；别的事正在活跃，就会把它压下去。时间按真实秒数走。
核里没有随机数，也没有发送权——掷骰子和放行都在核外。

### 3.2 三条规矩

**一、关系先写清楚。** 三份契约：Goal 写"为什么做、什么不算完成"，
Reward 写"成了算多少"，Candidate 写"做什么、什么时候自己失效"。
只加不改，历史改不动。

**二、做完要记账。** 期望在发出去之前就冻结；用户真回应了算实际；
窗口过了还没观察到算截断（**没回复不等于拒绝**）；后来发现记错了，加一条更正它。
发出成功只结算"送达"，不算用户反应。

**三、只有一个地方能发。** 发送权威是一个持久指针，不是配置开关。
每次派发先领凭证，没凭证发不出去；撤销立刻生效，不补发。
另有一个影子档位：照常推演，但发不出去，也不计收益。

### 3.3 它经常不说话

上线实测 `hazard ≈ 2.4e-05`。真正的失败是话太多，不是话太少。

---

## 4. 四条不变量

1. **原始事件永不改写。** 一切解释都是追加的新版本，历史字节不变。
2. **只有一个写者。** 模型输出只是**建议**，经 Reducer 判定 `APPLY / REBASE / DISCARD` 才能落地。
3. **`committed` 不等于 `sent`。** 决定要说、渲染完、真发出去是三件事，只有宿主回执才算发出。
4. **没有派发凭证就没有发送。** 权威指针 + 精确 claim 双重约束。

另有一条容易被忽略：**隐藏的心理上下文绝不进入永久对话历史**——宿主在无法保证"临时"时会拒绝注入，
而不是将就。

---

## 5. 仓库结构

```text
.
├── runtime/                    # ★ 持久认知 sidecar（独立进程，Python 3.11+）
│   ├── src/companion_runtime/  #   认知机制 + 「浪潮」+ 社会关系 + v1 兼容层
│   ├── tests/                  #   离线测试（见 §7）
│   ├── docs/                   #   上线记录与回滚手册
│   └── README.md               #   ★ 逐机制说明 + 配置 / API / 恢复 / 降级
├── framework/                  # 外接测试框架：可控时钟 + OpenAI 兼容 mock
├── scripts/                    # 验证、仿真、fleet 脚本
├── AstrBot/                    # 上游 AstrBot，零修改（不进镜像、不进 Git）
├── archive/                    # 已放弃的本地模型路线（留档）
├── CHANGELOG.md / HANDOFF.md   # 改动记录 / 换机器接手手册
└── LICENSE (AGPL-3.0-or-later) / RECOVERY.md
```

- **存储是 PostgreSQL-only**：每人一个 schema（`cr_<会话标识>`），迁移只追加，已应用的校验和不再改写。
- **本仓库由两个仓库组成**：这里是主程序；`astrbot_plugin_companion_runtime` 是宿主侧薄插件，
  **独立仓库、独立许可证**（要单独发插件市场），本地只是克隆副本。
- **设计与审查材料不在这个仓库里**：原始架构设计、架构补丁、设计→代码对照、提示词与角色卡、
  上线前大审查的清单与报告都已移除，只留运维手册。所以能复核的是代码、测试和这两份 README
  （`CHANGELOG`/`HANDOFF` 的历史条目仍会引用那些文件名，点了会 404——那是当时的事实，没有回改）。
- **`AstrBot/` 任何情况下都不修改**，所有集成走公开 API；升级只需替换该目录。

---

## 6. 部署

先按规模选一条路，两条路都只要一个**必填**值（PostgreSQL 的 DSN）：

| 规模 | 用什么 | 容器 |
|---|---|---|
| **一个人自己用** | 仓库根目录的 `docker-compose.yml`（Runtime + AstrBot + 插件挂载） | 2 |
| **几个到十几个人** | fleet：一台容器里**每人一个 Runtime 进程**（监督进程 + `fleet_admin.py` 开通/体检） | 3–4（fleet + PostgreSQL + AstrBot，可选 NapCat） |

单人的每一步在下面 §6.2–§6.6；多人的在 §6.8。**照着做能跑起来的那份完整流程**
（开通、体检、升级、回滚、陷阱清单）是 [`docs/部署与运维.md`](docs/部署与运维.md)。

### 6.1 需要什么

Linux + Docker（含 `docker compose`）、**PostgreSQL 18**（运行时存储**只支持 PostgreSQL**：
不配 DSN 会在启动时直接报错，而不是退回 SQLite）、AstrBot 4.28.x（上游零修改）、
一个 OneBot v11 前端（作者用 NapCat）。可选：一个 OpenAI 兼容的远端语义端点（§6.5，不配也能完整运行）。

### 6.2 一个人：两个容器

```bash
git clone https://github.com/bomomoQWQ/xiaojiujiu.git && cd xiaojiujiu
# 插件是独立仓库，compose 从 ./astrbot_plugin_companion_runtime 挂载，必须先 clone 到这个路径
# （不 clone 的话 Docker 会建一个空目录，AstrBot 起来后"没装插件"，而且不报错）
git clone https://github.com/bomomoQWQ/astrbot_plugin_companion_runtime.git

export CR_STORAGE__DSN='postgresql://user:pass@<pg 主机>:5432/<库>'   # 唯一必填
docker compose up -d --build

curl -s http://127.0.0.1:8787/health | head -c 200   # Runtime
# AstrBot WebUI：http://127.0.0.1:6185
```

compose 只把端口发布到 `127.0.0.1`（`8787` Runtime、`6185` WebUI）——**Runtime 没有面向公网的
鉴权设计，不要暴露出去**；要远程访问就在 WebUI 前面放反代。

### 6.3 让插件找到 Runtime（一次，别跳过）

WebUI 里把插件的 `runtime_base_url` 设成 **`http://runtime:8787`**（compose 里的服务名），
然后重载插件。插件默认值 `http://127.0.0.1:8787` 只在"插件与 Runtime 同机"时才对，
在容器里指的是 AstrBot 自己——**症状是状态行显示 `cognition: unavailable`，而她永远不说话**。

### 6.4 让她真的会开口：三道门（默认全关）

新部署默认是**静默**的（这是刻意的 fail-closed）。要她开口，三道门都得过：

```yaml
# 加在 docker-compose.yml 的 runtime 服务下（或写进 env_file / .env）
CR_LANGCHAO__LIVE_ENABLED: "true"
CR_LANGCHAO__LIVE_SCOPE_ALLOWLIST: '["default:FriendMessage:10001"]'  # 必须 JSON 数组
CR_LANGCHAO__SHADOW_ENABLED: "true"     # 可选：影子推演，零副作用
CR_LANGCHAO__SOCIAL_ENABLED: "true"     # 可选：社会关系投影
```

1. **live 引擎开着**（`CR_LANGCHAO__LIVE_ENABLED`，默认 false）；
2. **你的会话在白名单里**（`..._SCOPE_ALLOWLIST`，默认空）；
3. **数据库里的权威指针是 live**——它不是配置开关，是持久化的 CAS 指针；用
   `dispatch_authority.py show` 看、`freeze`/`restore` 改。

缺任何一条都是同一个症状：**"部署成功，她永远沉默"**。所以出问题先跑体检（§6.7），不要猜代码。
列表型配置必须是 JSON 数组：写成裸字符串会 fail-closed 拒绝启动（那些门禁是精确匹配，
字符串会退化成子串匹配）。

### 6.5 语义理解（可选；开关默认是开的）

`semantic.provider` 默认 `remote_api`：**"默认开"指的是开关，不是"自带模型"**。
端点、模型名、密钥是**你**填的输入，本仓库不预置厂商、不预置端点、不预置模型名。
三样齐全才 `available()`：

```bash
CR_SEMANTIC_BASE_URL='https://<你的端点>/v1'
CR_SEMANTIC_MODEL='<模型名>'
CR_SEMANTIC_API_KEY='<key：只从环境变量读，绝不写进任何仓库文件>'
```

不填 = 行为与"不接模型"完全相同：显式事件由规则表结算，模糊事件记为 `unresolved`，
心理解释退化为确定性模板，**长期连续性照常成立**（`/health` 会直接报
`provider: remote_api, available: false`，那不是故障）。想明确拒绝一切外呼就设
`CR_SEMANTIC_PROVIDER=disabled`。

### 6.6 夜里安静（一个旋钮，不是两个）

```bash
CR_LANGCHAO__NIGHT_SEND_PENALTY='0.85'          # 默认
CR_LANGCHAO__NIGHT_SEND_PENALTY_START_HOUR='0'  # 默认
CR_LANGCHAO__NIGHT_SEND_PENALTY_END_HOUR='5'    # 默认
```

窗口是**半开区间** `[start, end)`，所以 `0`–`5` 是**四**个受罚的小时。
它是**加价**不是硬停（系数被限制在 `< 1`：普通冲动过不去，理由足够强还是能开口）；
真的想**完全不发**用 `CR_SCHEDULER__QUIET_HOURS_START/END`（默认关，调度器根本不唤醒）。
"想安静"和"想完全不发"是两件事，所以它们是两个旋钮。

### 6.7 装完自检

```bash
# 单人：Runtime 活着吗、语义端口什么状态、有没有积压
curl -s http://127.0.0.1:8787/health | python3 -m json.tool | head -30

# fleet：谁在服务、能不能发
RT() { docker exec xxj-runtime-fleet python "/fleet/$1" "${@:2}"; }
RT fleet_admin.py list
RT fleet_admin.py check default:FriendMessage:10001   # 逐条说明"她为什么不主动"
```

**查正在跑的是哪个版本**：`GET /health` → `runtime_version`（同一个响应里的 `state_version`
是状态计数器，不是版本号）。仓库侧的版本号在四处保持一致，
`python3 scripts/check_versions.py` 不一致就退出码 1。

### 6.8 多个人：fleet

一台容器里**每人一个 Runtime 进程**（一个 Runtime 只承载一个角色、一份状态，所以隔离是按进程，
不是按会话）。生成 compose 与人员名单：

```bash
python3 scripts/build_runtime_fleet.py --people 10001,10002 --project <项目名>
# 它同时会把舰队拉起来（--no-start 可只渲染）；端口在 compose 网络内，不发布到宿主机
```

开通与下线走体检工具（它会**断言** schema 真的迁移到本构建期望的版本）：

```bash
RT fleet_admin.py add default:FriendMessage:10003   # 建 schema、跑迁移、注册、等健康
RT fleet_admin.py check default:FriendMessage:10003
RT fleet_admin.py remove <slug>                     # 默认不删数据；--purge-schema/--purge-data 才删
```

运行时**每人一个 schema**（`cr_<会话标识>`），规则只有一份定义（`scripts/fleet_naming.py`），
监督进程与运维工具都从那里取——这条规则漂移的表现就是"行写进了没人读的 schema"。

### 6.9 端口与密钥

端口默认只发布到 `127.0.0.1`。**不要把 Runtime 暴露到公网**：它没有面向公网的鉴权设计，
远程访问请在 AstrBot WebUI 前放反代。语义 key 只从环境变量读（`CR_SEMANTIC_API_KEY`），
不落盘、不进日志、不进 `/health`（只报 `configured` / `not configured`）。

### 6.10 自部署：现在的状态

**定位仍然是"作者自己能长期跑得住"优先**，不是"开箱即用"。但"别人部署不了"这件事已经被逐条消掉，
据实记录如下——上面 §6.2–§6.8 就是这些能力的用法。

**已经能做到了：**

| 能力 | 怎么用 |
|---|---|
| 开通一个人并**验证** | `fleet_admin.py add <session>`：分配端口/schema、写名单、起进程、等健康，**并断言 schema 真的迁移到本构建期望的版本**（少了这步会得到"Runtime 健康但写进了没人读的 schema"） |
| 诊断"她为什么不主动" | `fleet_admin.py check [scope]`：逐条点出——没有 runtime 服务这个会话 / 运行时不允许主动 / 不在 live 白名单 / 权威被冻结 / schema 缺失或落后 / 有卡住的行 |
| 看清谁在服务 | `fleet_admin.py list`：端口、健康、`may send`、是否在 live 白名单、该用的 schema 名 |
| 名单不再写死在代码里 | 运维脚本从 `--scopes` / 环境变量 / 人员名单文件 / 控制面解析；拿不到就**明确拒绝**，不会照猜的名单乱动 |
| 会话→schema 规则只有一份 | `scripts/fleet_naming.py`；监督进程和运维工具都用它，避免两处漂移（漂移的表现是"行写到了没人读的地方"） |
| 无中断升级 | 换 site-packages 里的包 + `rolling_restart.py --confirm`，逐 scope 滚动；见 [`docs/部署与运维.md`](docs/部署与运维.md) §6.2 |
| 部署手册 | [`docs/部署与运维.md`](docs/部署与运维.md)：拓扑、首次部署、开通、开关、升级、回滚、陷阱清单 |
| 版本号不再各说各话 | 一处号、四处同步（`CHANGELOG.md` 顶部 / `runtime/pyproject.toml` / `runtime/src/companion_runtime/__init__.py` / 插件 `metadata.yaml`），`scripts/check_versions.py` 不一致就退出码 1。**查正在跑的是哪个版本**：`GET /health` → `runtime_version` |
| 有 tag 可回滚 | 两个仓库都打了 `v0.5.0`（此前从无任何 tag）；插件另有按 tag 打的 zip 可直接用 WebUI 上传 |
| 文档与现状一致 | `runtime/README.md` §10 从「SQLite 时代」改写为 PostgreSQL 现实（DSN/schema、迁移 v26、备份用 `pg_dump`、Runtime 的耐久性命令在 PG 上**明确拒绝**）；依赖行、模块树、配置示例、环境变量示例、CLI/HTTP 表里的 SQLite 说法一并改掉 |

**还没做：**

| 缺口 | 现状 |
|---|---|
| 插件不在 AstrBot 插件市场 | 市场发布**已冻结**（有意，见 `HANDOFF.md` 的「已冻结」一节）。版本化发布物已经有了：tag + zip + 发版流程，只是不走市场 |
| `scripts/` 里两百多个历史一次性脚本 | 仍写死作者的路径与当时的 QQ 号。**当前工具与历史的区分**写在 [`scripts/README.md`](scripts/README.md)，而当前工具由 `runtime/tests/test_ops_scripts_are_portable.py` 守着（写死 `/srv/companion`、`F:\…`、`E:\…` 会直接失败）；历史脚本按约定保留原样，没有回改 |
| `HANDOFF.md` | 2026-09-15 ~ 09-23 的封测期流水账，是当时的事实，**不要当作当前部署指南** |
| 首次部署仍要手写两样 | `.secrets/runtime-v2-postgres.env`（库口令）与基础 compose `astrbot.yml`（镜像、语义密钥等服务定义都从它复制）。生成器本身会把舰队拉起来（`docker compose up -d`，`--no-start` 可只渲染），所以不会漏步；但"手写 compose"这条还在 |

**结构性的事实（是取舍，不是欠账）：**

| 事实 | 说明 |
|---|---|
| 没有 CI | 镜像自己 build，没有镜像仓库、没有镜像 tag（git 侧现在有 `v0.5.0`） |
| **不是多租户** | 状态单租户全局；多人要"一人一个 Runtime"（fleet 就是这么做的），那是运行期做法 |
| 开关不少 | 语义端口、时间尺度、价值观轴、心跳间隔、发送权威……默认能用，想调得先理解 |

**为什么会这样**：精力一直在"认知机制对不对"——每条机制都要有测试、仿真和反证，
这部分吃掉了绝大部分时间。部署打磨（向导、发布、文档去本机化）是明确的欠账，**不是有意留的坑**。
真要自部署：先读 `runtime/README.md` 的弱 VPS 建议与快速自检两节，确认服务真的活着。

---

## 7. 验证

**能当门禁跑的**（聚焦套件，当前 `207 passed / 1 skipped`）：

```bash
cd runtime
python -m pytest -q tests/test_langchao_types.py tests/test_langchao_engine.py \
  tests/test_langchao_reward.py tests/test_langchao_authority.py \
  tests/test_langchao_runtime_adapter.py tests/test_langchao_live.py \
  tests/test_langchao_shadow_wiring.py tests/test_langchao_social_repository.py \
  tests/test_langchao_exploration.py tests/test_langchao_permission_and_no_send.py \
  tests/test_langchao_user_outcomes.py tests/test_langchao_history_closed_loop.py \
  tests/test_actual_action_v21.py tests/test_capability_witness.py \
  tests/test_render_plan_v1.py tests/test_privacy_deletion.py \
  tests/test_pf011_goal_lifecycle.py tests/test_goal_terminal_event_producer_wiring.py
```

**PostgreSQL 专项套件**（需要 DSN）覆盖只有真库才能证明的东西：迁移幂等、精确外键、
权威 CAS 并发只有一位赢家、回调失败整事务回滚、并发 ACK exactly-once、重启恢复不派发、
跨作用域拒绝、单所有者事务：

```bash
CR_TEST_PG_DSN='postgresql://...' python -m pytest -q \
  tests/test_live_dispatch_legacy_postgres.py tests/test_langchao_prelaunch_postgres_audit.py \
  tests/test_langchao_repairs_postgres.py tests/test_transaction_ownership_postgres.py
```

**反证工具**（证明检查会咬人）：`scripts/mutation_design_conformance.py` 把 bug 放回去看测试红不红；
`dead_code_inventory.py` 找没人读的东西；`business_logic_probes.py` 是已修缺陷的改前/改后对照。

> **`pytest tests` 整体现在不是全绿**：`2050` 个用例里 **196 失败 + 33 错误**（2026-10-07 实测）。
> 原因不是逻辑回归，而是**夹具欠账**——v2 之后存储要求 PostgreSQL，
> 而一批历史测试仍用 `config.storage.database_path = "….sqlite3"` 直接构造 Runtime，
> 于是必然抛 `ValueError: PostgreSQL storage is required`；给 DSN 也没用，那些测试不读环境变量
> （实测：配上连不上的 DSN 再跑，失败数量一模一样）。
> 修法只有两条：把夹具迁到 PostgreSQL schema，或显式 skip 并写明理由。这件事没做。

---

## 8. 已知边界

| 边界                               | 影响                                                                                                                                                                                                                                 |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **不是开箱即用**         | 自部署要自己准备 PostgreSQL、基础 compose 与库口令（见 §6.10 的「做了 / 还没做」逐条清单）。运维工具、逐条自检、版本化发布与部署手册都已经有了 |
| **最终措辞不作为预测特征**   | 进模型的是发送前冻结的「渲染计划」结构（要不要回复、压力等级、长度档、模板/风格版本）；**实际文本只做审计溯源**。所以学的是"计划类型 + 上下文 → 用户反应"，不是"具体句子 → 用户反应"。这是刻意避免 post-treatment 泄漏的代价 |
| **注意力配方默认 off**       | 全 1 权重、无竞争。B2/B3 已实现且有隔离正控，但未授权给任何作用域                                                                                                                                                                    |
| 主动消息非常稀疏                   | `hazard ≈ 2.4e-05`，低打扰是设计目标                                                                                                                                                                                              |
| 隐私删除默认关闭                   | 协调器、租约、跨派生物失效都已实现并有 PG 证据，但 HTTP 路由未挂载                                                                                                                                                                   |
| 记忆检索是词法降级                 | 无 embedding，语义相近但用词不同的记忆检索不到                                                                                                                                                                                       |
| 送达与上报之间有崩溃窗口           | 恰好两步之间被杀时只能靠租约重投，理论上会重复一条主动消息                                                                                                                                                                           |
| 逾期未回复的已发送尝试不做过期清理 | 那会凭空捏造历史；它只由用户回复或边界关闭                                                                                                                                                                                           |
| 上游回执不可靠                     | 渠道失败/回执未知有明确分支与 no-send 原因审计，但 exactly-once 仍受前端能力限制                                                                                                                                                     |

---

## 9. 许可证

**AGPL-3.0-or-later**（全文见 `LICENSE`）。Copyright (C) 2026 bomomoQWQ。

**商业许可另议**：需要闭源集成、或无法承担第 13 节义务的商业部署，可按 `LICENSE` 末尾的方式洽谈。

---
