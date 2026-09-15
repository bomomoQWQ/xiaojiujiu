# 小九九 · 内源主动型长期陪伴 AI Runtime

一个**跨时间连续**的陪伴角色系统：宿主 Bot 负责"现在这一刻怎么回应"，
小九九负责"这一刻过去以后留下什么"，以及"要不要开口"。

名字取自"心里的小九九"：它确实整天在打小九九——候选意图、效用打分、危险率、
边界成本，排队算出"现在说这句话值不值"。仓库名 `xiaojiujiu`，旧代号「理解痞老板」。

设计目标不是让机器人更会聊天，而是让它**记得住、沉得下、会自己想开口**，
并且**不会因此发疯**——不骚扰、不越界、不把沉默误读成恶意、不忘掉发生过的事。

---

## 1. 它解决什么问题

普通聊天机器人的"人格"活在上下文窗口里：窗口一滑，上一轮的情绪、承诺、
没说完的事全部消失。于是它永远只有"此刻"，没有"这几天"。

本项目把状态从上下文里搬出来，放进一个独立的持久认知进程：

```text
用户消息 ──► 宿主 Bot（AstrBot + 主 LLM）
                │  即时演出：直接看原话 + 上下文 + 人格，当场回应
                │
                └──► Runtime sidecar（本仓库核心）
                       持久认知：情绪余波 / 记忆 / 未尽之事 /
                                 用户模型 / 候选意图 / 主动动力
                       │
                       └──► 未来轮次：上下文注入 + 主动发消息
```

关键在于**两层的职责绝不重叠**：

| | 即时演出层 | 持久认知层 |
|---|---|---|
| 谁负责 | 宿主主 LLM | Runtime |
| 回答什么 | "用户现在说了这句话，我这一刻怎么反应？" | "这件事结束以后，它在我身上留下了什么？" |
| 时间尺度 | 毫秒～秒 | 小时～天 |
| 是否依赖模型 | 是（就是主 LLM 本身） | **否**（确定性代码） |

---

## 2. 三条不变量

整个系统的价值都压在这三条上，它们各自有专门的回归测试：

1. **原始事件永不改写。** 一切解释都是追加的新版本，历史字节不变。
   → 所以"当时没理解、后来才想明白"是可能的，而不是把过去偷偷重写。
2. **只有一个写者。** 所有模型输出都只是**建议**，经 Reducer 判定
   `APPLY / REBASE / DISCARD` 才能落地。没有任何模型有写权限。
3. **`committed` 不等于 `sent`。** 决定要说、渲染完、真发出去是三件事，
   只有宿主回执才算发出。显式边界在效用计算**之前**剪枝，不进博弈——
   压力再大也不能越过"别来找我"。

---

## 3. 仓库结构

```text
.
├── runtime/                          # ★ 持久认知 sidecar（独立进程，Python 3.11+）
│   ├── src/companion_runtime/        #   29 个模块（含协议 v1 兼容层 api_v1.py）
│   ├── tests/                        #   807 项离线测试
│   ├── docs/PATCH_V0.2_MAPPING.md    #   设计章节 → 代码位置 → 状态（含诚实缺口清单）
│   └── README.md                     #   操作者手册（配置 / API / 蓝屏恢复 / 降级）
│
├── astrbot_plugin_companion_runtime/ # ★ AstrBot 薄插件（宿主侧唯一改动）
│   ├── main.py                       #   监听 / 临时注入 / outbox 消费
│   └── tests/                        #   143 项离线测试（另含 13 个子测试，带 AstrBot 桩）
│
├── AstrBot/                          # 上游 AstrBot 4.28.1，**零修改**
│
├── archive/                          # 已放弃的本地模型路线（留档，不参与构建）
│   └── README.md                     #   为什么放弃 + 实测数据
│
├── scripts/                          # 运维与验证脚本
│   ├── backup.ps1                    #   跨盘原子快照（蓝屏防护）
│   ├── runtime_bench.py              #   关键路径延迟基准
│   ├── e2e_patch_v02.py              #   28 项基础真机验证
│   └── e2e_resilience_simulation.py  #   高仿真并发、重启与故障恢复验证
│
├── 内源主动型长期陪伴AI_Runtime_完整架构设计.md   # 原始设计（97 节）
├── PATCH_v0.2_即时演出与持久认知分离...md        # 现行架构补丁
└── RECOVERY.md                       # 备份 / 恢复 / 权重位置
```

**`AstrBot/` 是上游代码，任何情况下都不修改。** 所有集成通过公开 API 完成，
升级 AstrBot 只需替换该目录。详细边界见 `runtime/README.md` 与插件 README。

---

## 4. 快速开始

### 4.1 启动 Runtime

```powershell
cd F:\理解痞老板\runtime

# 建独立环境（不污染 AstrBot 的环境）
uv venv .venv --python 3.12
uv pip install --python .venv\Scripts\python.exe -e ".[test]"

# 自检
.venv\Scripts\python.exe -m pytest tests -q          # 期望 807 passed

# 起服务（默认 127.0.0.1:8787）
.venv\Scripts\python.exe -m companion_runtime.cli serve --db runtime\data\companion.db
```

`serve` 只监听 loopback，并且**不需要任何模型**——默认配置下
`SemanticProvider` 是 `disabled`，Runtime 完全靠确定性代码工作。

### 4.2 安装薄插件

```powershell
# 复制到 AstrBot 的插件目录（也可用 WebUI 上传 zip）
Copy-Item -Recurse astrbot_plugin_companion_runtime AstrBot\data\plugins\ -Force
```

然后在 AstrBot WebUI 里确认插件的 `runtime_base_url` 指向 Runtime 地址（插件与
Runtime 的当前默认值均为 `http://127.0.0.1:8787`）；若 Runtime 改过监听地址，再同步修改该项并重启 AstrBot。

> **多会话部署必做**：把 Runtime 的 `conversation_id` 设成会话的
> `unified_msg_origin`（如 `aiocqhttp:FriendMessage:10001`）。
> 主动消息会投递到「形成这个意图的会话」，而 Runtime 需要知道自己是哪个会话——
> 只在单会话下用默认值才是安全的。相关推导与测试见
> `runtime/tests/test_proactive_routing.py`。

### 4.3 验证

```powershell
# 真机端到端（会起一个真实 HTTP 服务并打真实请求）
python scripts\e2e_patch_v02.py --base-dir E:\companion_runtime_backup\e2e-v02

# 关键路径延迟
python scripts\runtime_bench.py --rounds 200
```

---

## 5. Runtime 的 HTTP 接口

47 条路由（完整表格见 `runtime/README.md`）。分两套契约：

**内部接口（Runtime 自己的形状）**

| 分组 | 代表端点 | 用途 |
|---|---|---|
| 事件 | `POST /events`、`GET /events/{id}` | 追加原始事件、读取历史 |
| 上下文 | `POST /context/render-block`、`POST /explain` | 生成**本轮临时**注入块 |
| 主动发送 | `POST /outbox/claim`、`POST /render`、`POST /delivery` | 租约 → 渲染 → 投递三段式 |
| 授权 | `POST /authorize` | 发送前最后一道闸门（fail-closed） |
| 认知 | `POST /cognition/refresh`、`GET /cognition/backlog` | 低频深层刷新与未解释积压 |
| 运维 | `GET /health`、`POST /maintenance/*` | 健康、校验、检查点、备份、恢复 |

**协议 v1（薄插件使用的形状）**

插件走 `/v1/events`、`/v1/context`、`/v1/outbox/lease`、
`/v1/outbox/{id}/heartbeat`、`/v1/actions/{id}/authorize`、`/v1/outbox/{id}/result`
六条路径，由 `runtime/src/companion_runtime/api_v1.py` 翻译到内部接口。

两套契约的分工是刻意的：v1 是**宿主适配契约**（租约、心跳、发送前授权、结果幂等），
内部接口是 Runtime 自己的资源形状。翻译层负责幂等、fail-open / fail-closed 分类，
以及保证 `committed != sent` 在跨进程边界上依然成立。

---

## 6. 持久认知里到底有什么

| 机制 | 做什么 | 代码 |
|---|---|---|
| `lazy_tick(now)` | 闭式时间推进：心境、情绪衰减、I/R/P 动力学、冷却、期限 | `runtime.py` |
| 粗粒度语义结算 | 显式事件给出方向 + 强度档；**模糊事件留 `unresolved` 不猜** | `semantic.py` |
| 情绪余波 | 评级 → 数值影响 → 衰减，带余味 | `emotion.py` |
| 边界状态机 | 4 类边界，硬闸门优先于一切动机 | `boundaries.py` |
| 未尽之事 | 检测 / 等待 / 到期 / 了结的状态机 | `unfinished.py` |
| 记忆 | 候选评分 → 巩固 → 激活 → 检索（当前为词法降级） | `memory.py` |
| 用户模型 | 在线加权 Logistic + 分层部分池化 + 保守分位数 | `user_model.py` |
| 候选意图 | 生成 → 接地校验 → 池管理（ADD/UPDATE/RETIRE） | `candidate.py`、`pool.py` |
| 动机博弈 | 7 项效用分解、沉默效用、**危险率**触发、softmax 选择 | `motivation.py` |
| 深层刷新 | 低频回头理解旧事件，建议集经接地校验后交 Reducer | `deep_refresh.py`、`providers.py` |

**为什么用危险率而不是阈值**：阈值会让 `0.799` 和 `0.801` 产生完全不同的行为，
而且改心跳频率就改行为。危险率 `λ(t) = λ₀·softplus(β·D)` 把决策变成时间的连续函数，
心跳快慢不影响期望行为——这一点有专门的测试守着。

---

## 7. 关于模型

**Runtime 不依赖任何生成式模型。** 默认 `SemanticProvider = disabled`，
所有认知由确定性代码完成；关键路径 `p50 ≈ 1 ms`。

需要更强语义时，可选接一个 **远端** OpenAI 兼容端点，只用于**低频深层认知刷新**
（重解释旧事件、心理状态语言化），永远不在即时路径上：

```powershell
$env:CR_SEMANTIC_PROVIDER = "remote_api"
$env:CR_SEMANTIC_BASE_URL = "https://api.example.com/v1"
$env:CR_SEMANTIC_API_KEY  = "..."      # 只从环境变量读，绝不落盘/落日志
```

> **本地模型路线已被放弃。** 实测在目标硬件上单次评价要数秒、常驻约 2 GB，
> 且最小量化也无法在 1 GB VPS 上运行；架构上它也与主 LLM 重复。
> 原因、实测数据与留档位置见 `archive/README.md`。
> 环境里若仍导出 `local_cpu` / `local_gpu` 等退役名字，Runtime 会打印一条
> WARNING 说明该路线已移除，然后回落到 `disabled`——不会静默失败。

---

## 8. 目标硬件

设计目标是**便宜、长期稳定、可维护**的部署：

| 组件 | 常驻内存 | 说明 |
|---|---:|---|
| Runtime sidecar | **约 29 MiB** | 纯 Python + SQLite，无 GPU |
| AstrBot + 主 LLM | 取决于宿主 | 由 AstrBot 自身决定 |
| 数据库 | 单文件 SQLite | WAL 模式 |

实测（i7-13700H，200 轮完整对话）：

```text
ingress    p50 0.95 ms   p95 1.32 ms
lazy_tick  p50 0.38 ms   p95 0.56 ms
CPU        0.45 s / 200 轮
```

**弱 VPS 上不需要跑模型**——这也正是放弃本地路线的原因之一。

---

## 9. 数据安全与蓝屏恢复

蓝屏/断电是 Windows 上的现实风险，所以持久化按"最坏情况"设计：

- SQLite **WAL + `synchronous=NORMAL` + `BEGIN IMMEDIATE`**：断电可能丢掉最后几个已提交事务，
  但**数据库永不损坏**（WAL 帧校验和不通过就丢弃尾部）。
- 一个完整认知轮 = 一个事务：不会出现"改了一半"的状态。
- `backup` 用 SQLite 在线备份 API，可在**不停机**的情况下拿到一致快照，
  先写临时文件再原子重命名。
- `verify` 除 `integrity_check` 外还查 6 项结构一致性（悬空引用、孤儿转移、
  无过期时间的租约等），损坏时退出码 3 而不是抛裸异常。

```powershell
# 备份（默认不覆盖，可 --keep N 保留最近 N 份）
companion-runtime backup  --db runtime\data\companion.db --output E:\backups\latest.db
companion-runtime verify  --db runtime\data\companion.db
companion-runtime recover --db runtime\data\companion.db    # 给出恢复建议
```

跨盘快照与完整恢复流程见 `RECOVERY.md`；源码快照在 `E:\companion_runtime_backup\`。

---

## 10. 测试

```powershell
cd runtime
.venv\Scripts\python.exe -m pytest tests -q          # 807 passed

cd ..\astrbot_plugin_companion_runtime
$env:PYTHONPATH="$PWD\tests\stubs;$PWD\.."
..\.venv-dev\Scripts\python.exe -m pytest tests -q   # 143 passed + 13 subtests

cd ..
python scripts\e2e_patch_v02.py                      # 28 项基础真机检查
python scripts\e2e_resilience_simulation.py --base-dir E:\companion_runtime_backup\resilience-final
                                                     # 335 项高仿真检查（并发 / 重启 / 断网恢复）
```

测试覆盖的重点不是行数，而是**几类容易悄悄坏掉的东西**：

- **不变量**：原始事件不可改、推断不能变成事实、边界压过一切动机、
  `committed ≠ sent`、隐藏上下文不进永久历史、每个入口都先 `lazy_tick`。
- **性质**：tick 分解一致性、危险率与心跳频率无关、分层退化、softmax 熵单调。
- **降级**：模型不可用/超时/返回垃圾/返回非法 JSON 时，系统必须继续工作。
- **回归**：每个被真实运行抓到过的缺陷都有一条对应的测试。例如：
  - 一次深层刷新只结算它**真正引用过**的事件，无关积压保持不变；
  - 主动消息必须投递回**形成该意图的会话**，而不是进程默认会话；
  - 未尽之事的去重按主题而非单字，否则「面试」和「考试」会因共用「试」而被合并；
  - 插件状态命令读的字段名必须与 Runtime 实际返回的一致
    （这条曾因测试桩用了另一个名字而漏过）；
  - `serve` 必须真的把内源调度跑起来（曾经只监听了 HTTP，角色在标准部署下永不醒来）；
  - 一条主动消息只算一次当日接触，且只在**投递成功**时计；
  - 用户的回复只结算**最新一条已发出**的 attempt，且只结算一次；
  - 授权环节断网（拿不到裁决）不能被当成"业务拒绝"而把意图判死；
  - `POST /rendered` 在积压超过 100 行时仍要找到本次 attempt 的行，而不是退化成直接路径；
  - 报表重复、并发重复、进程重启都必须是幂等的。

`scripts/e2e_resilience_simulation.py` 用真实 uvicorn + 真实文件 SQLite(WAL) + 真实插件
传输层跑 335 项检查，覆盖并发上报、租约过期、断网恢复、多会话路由与重启续跑——
单元测试证明"这条路径对"，它证明"这套部署在坏天气下也对"。

`runtime/docs/PATCH_V0.2_MAPPING.md` 把设计文档的每一节映射到代码位置与状态，
并**如实列出未实现的部分**——那份清单比测试数量更能说明现在到哪了。

---

## 11. 已知边界

诚实列出，避免误以为已经完备：

| 边界 | 影响 |
|---|---|
| 记忆检索是词法降级，无 embedding | 语义相近但用词不同的记忆检索不到 |
| 没有常驻记忆巩固 worker | 巩固由调用方驱动 |
| 深层刷新的触发信号部分需调用方提供 | `major_event` / `history_suspect` 等 Runtime 无法自行判断，默认按"不成立"处理 |
| `user_model_evidence` 只存为解释版本 | 不并入数值用户模型——那只能由真实交互观测训练，否则模型猜测会覆盖实测行为 |
| 无 Prometheus 导出 | 运维需自己抓 `/health` |
| 单租户全局状态 | 多会话共用一个 `runtime_state`，`conversation_id` 已贯穿全表，拆分留给后续 |
| 「平台已发出」与「结果已上报」之间存在崩溃窗口 | 插件在发出前不写结果，若恰好在两步之间被杀，Runtime 只能靠租约到期重投，理论上会重复一条主动消息；真正消除需要平台投递回执或宿主持久化幂等日志 |
| 授权环节断网时插件保持沉默 | 不写任何结果（fail-closed 也 fail-silent），靠 Runtime 的租约到期回收重投；这会把长时间断网消耗在 attempt 预算上，预算耗尽后按设计终止 |
| 逾期未回复的 `sent` attempt 不做过期清理 | `sent→resolved` 是唯一合法迁移，超时会凭空捏造历史；它只由用户回复或边界关闭 |

---

## 12. 从这里往下读

| 想了解 | 读 |
|---|---|
| 原始设计意图（97 节） | `内源主动型长期陪伴AI_Runtime_完整架构设计.md` |
| 现行架构与改动理由 | `PATCH_v0.2_即时演出与持久认知分离_移除本地2B核心依赖.md` |
| Runtime 怎么配、怎么运维 | `runtime/README.md` |
| 设计 → 代码 → 状态对照 | `runtime/docs/PATCH_V0.2_MAPPING.md` |
| 插件侧契约与隐私边界 | `astrbot_plugin_companion_runtime/README.md` |
| 备份、恢复、权重位置 | `RECOVERY.md` |
| 为什么不做本地模型 | `archive/README.md` |
