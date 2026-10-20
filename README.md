# 小九九 Runtime

让陪伴角色拥有**跨时间连续的内在状态**：记忆、情绪余波、未尽之事、用户模型、关系状态、候选意图，以及「浪潮」主动决策。

宿主聊天框架负责“这一刻怎么说”；本 Runtime 负责“这件事留下什么”和“现在要不要开口”。它是独立 HTTP sidecar，不修改 AstrBot 上游，也不把认知状态塞进插件。

> 当前版本：`0.5.0` · Runtime API：`v1` · 许可证：AGPL-3.0-or-later

## 能做什么

- 持久保存原始事件、记忆、情绪、边界、未尽之事和关系投影；
- 把当前相关状态渲染为**只对这一轮有效**的认知块，隐藏心理内容不进入永久聊天历史；
- 用「浪潮」在多个候选意图与沉默之间连续竞争，决定是否主动联系；
- 把“决定发送”“渲染完成”“平台实际送达”“用户后来回应”分开记账；
- 可选使用任意 OpenAI-compatible 端点做低频深层理解；端点不可用时自动退回确定性规则与模板；
- 一个人一个 Runtime / PostgreSQL schema；多人部署由 fleet supervisor 管理；
- 提供开通、体检、冻结、滚动重启、schema 迁移与版本检查工具。

## 架构与边界

```text
聊天平台 / OneBot
        │
      AstrBot ── 薄插件 astrbot_plugin_companion_runtime
        │                  只负责上报、临时注入、渲染和发送
        │ HTTP Runtime API v1
        ▼
 companion-runtime
   ├─ 原始事件与机械投影
   ├─ 情绪 / 记忆 / 未尽之事 / 用户模型
   ├─ 社会关系与候选意图
   ├─ 浪潮决策、发送权威、outbox
   └─ PostgreSQL（每人一个 schema）
```

边界固定如下：

1. **本仓库只负责 Runtime。** AstrBot 和插件在 AstrBot 侧安装、升级、配置；本仓库不 clone、不挂载、不修改它们。
2. **原始事件追加不改。** 新解释只能追加并指向旧版本，不能回写历史。
3. **模型只给建议。** 所有状态变更仍由 Runtime 校验来源、版本、边界和契约后提交。
4. **没有派发凭证就不能发送。** 配置开关、精确 scope 白名单和持久化权威指针缺一不可。
5. **秘密不进配置文件。** PostgreSQL DSN 与 API key 只从 `.env` / 进程环境读取。

## 一个人部署

### 1. 准备

需要：

- Linux + Docker Compose v2；
- PostgreSQL 18；
- 已运行的 AstrBot（如需接聊天平台）；
- 可选的 OpenAI-compatible 语义端点。

Runtime 运行时只支持 PostgreSQL；没有 DSN 会直接拒绝启动，不会退回 SQLite。

### 2. 创建配置

```bash
git clone https://github.com/bomomoQWQ/xiaojiujiu.git
cd xiaojiujiu
cp config/runtime.example.toml config/runtime.toml
cp .env.example .env
```

编辑：

- `config/runtime.toml`：会话、完整 Runtime Persona、8 个价值观轴、行为、浪潮、夜间策略、语义端点/模型与分阶段提示词；
- `.env`：`CR_STORAGE__DSN` 与 `CR_SEMANTIC_API_KEY`。

本地 `config/runtime.toml` 与 `.env` 都被 Git 忽略；仓库只跟踪脱敏模板。

### 3. 启动 Runtime

```bash
docker compose up -d --build
curl -s http://127.0.0.1:8787/health | python3 -m json.tool
```

根目录 compose **只运行 Runtime**，并只把 `8787` 发布到 `127.0.0.1`。Runtime 没有公网鉴权，不要直接暴露到互联网。

### 4. 在 AstrBot 安装插件

在 **AstrBot WebUI → 插件管理**安装 `astrbot_plugin_companion_runtime`，然后在插件配置中填写 `runtime_base_url`：

| 部署关系 | URL |
|---|---|
| AstrBot 与 Runtime 已加入同一 Docker 网络 | `http://runtime:8787` |
| AstrBot 运行在 Runtime 所在宿主机 | `http://127.0.0.1:8787` |
| AstrBot 在另一台内网机器 | 你的内网 Runtime URL |

插件不在市场时，使用插件仓库 release 的 zip / URL 通过 AstrBot 安装；不要把插件 clone 到本仓库。

## 配置

加载优先级：

```text
代码默认值 < config/runtime.toml < CR_ 环境变量 < 少量 CLI 参数
```

Runtime 从仓库根目录启动时会**自动发现** `config/runtime.toml`；没有该文件就使用代码默认值与环境变量。
显式 `--config <path>` 始终优先，而且路径不存在会直接报错。常规部署只需改 TOML；环境变量用于秘密、
容器坐标和临时覆盖。嵌套环境变量用双下划线，例如：

```bash
CR_LANGCHAO__LIVE_ENABLED=false
CR_SEMANTIC__MODEL=another-model
```

旧的 flat `CR_SEMANTIC_PROVIDER`、`CR_SEMANTIC_BASE_URL`、`CR_SEMANTIC_MODEL` 等名字仍兼容；规范写法是 `CR_SEMANTIC__*`。`CR_SEMANTIC_API_KEY` 始终只由 provider 从环境读取，不进入 RuntimeConfig、日志或 `/health`。

完整、安全的起点见 [`config/runtime.example.toml`](config/runtime.example.toml)；参数分级与“不建议改”的
完整清单见 [`config/README.md`](config/README.md)。

### 语义端点

```toml
[semantic]
provider = "remote_api"       # remote_api | disabled
base_url = "https://api.example.com/v1"
model = "your-model-name"
timeout_s = 30.0
max_tokens = 1024
json_mode = true
```

`remote_api` 是默认**开关**，不是内置厂商。只有 `base_url + model + CR_SEMANTIC_API_KEY` 三者齐全时才会外呼；否则 `/health` 报 `available: false`，Runtime 继续使用规则与确定性模板。要明确禁止任何语义外呼，设 `provider = "disabled"`。

### 完整 Persona、阶段提示词与 AstrBot Persona

`[persona]` 是 Runtime 侧的**完整人格设定**，可全部重写：

```toml
[persona]
identity = "你是谁、如何理解自己。"
voice = "说话方式、句长、标点、口语程度。"
relationship = "如何理解与用户的关系、亲密与边界。"
shared = "所有阶段共同遵守的人物要求。"
```

配置了 `[persona]` 后，它取代 Runtime 内置的兼容人物风格，并同时进入：普通认知块、主动消息渲染、
深层理解、心理解释。AstrBot 主 Persona 仍归 AstrBot 管理，控制即时聊天的主角色；部署者应让两份 Persona
表达同一个人物，但本仓库不会读取或修改 AstrBot 配置。

四个阶段字段在完整 Persona 上继续细化：

```toml
[prompts]
conversation_context = "普通聊天阶段的额外要求。"
proactive_render = "主动开口阶段的额外要求。"

[semantic.prompts]
deep_refresh = "后台重解释和记忆整理的额外要求。"
explain_state = "心理状态转成语言的额外要求。"
```

Persona 可以完全自定义，**协议层不能覆盖**：用户边界、来源 grounding、时间校正、防复读、发送权威、
JSON 字段和“只输出正文”仍由代码强制。参考模板已给出一份可工作的 Persona；不建议把协议、数据库字段
或工具说明写进 Persona，也不建议复制 Runtime 内置系统提示词。

### 基础价值观参数

8 个轴取值 `0.0–1.0`，影响真实动力学而不只是文风：

```toml
[values]
autonomy = 0.72
boundary_respect = 0.88
emotional_expression = 0.46
relationship_maintenance = 0.79
user_care = 0.85
conflict_directness = 0.41
stability_commitment = 0.81
curiosity = 0.76
```

| 轴 | 主要影响 |
|---|---|
| `autonomy` | 自我推进，以及部分情绪敏感度的抑制 |
| `boundary_respect` | 边界成本、拒绝后的收敛 |
| `emotional_expression` | 情绪进入反应与表达的强度 |
| `relationship_maintenance` | 长期沉默后靠近、关系记忆显著性 |
| `user_care` | 用户未尽之事对角色的推动力 |
| `conflict_directness` | 把矛盾挑明或修复的倾向 |
| `stability_commitment` | 抗单次波动、稳定知识权重 |
| `curiosity` | 追问和了解的驱动 |

价值观只在**新实例首次创建 Runtime state** 时播种。已有实例已经持久化自己的 values；修改 TOML 不会
静默改人格，需要显式迁移。Runtime 会校验范围，任何轴超出 `0–1` 都直接拒绝启动。

### 哪些外部参数应该改

- **必须给**：PostgreSQL DSN；多人部署的 `conversation_id` 与 schema；
- **启用远端语义时给**：base URL、model、API key；
- **适合个人自定义**：Persona、8 个 values、live 白名单、夜间/quiet hours、每日主动预算、
  同一记忆开场冷却、观察文本开关；
- **按运维需要改**：host/port/log level、调度间隔、深层刷新频率；
- **不建议在个人模板调整**：emotion/drive/silence/utility 的动力学系数、学习率、candidate bias、
  浪潮 leak/threshold/time scale、outbox lease/attempt 等校准内核。

测试/旧兼容字段、隐私删除凭据摘要和内部实验开关不属于个人配置。完整字段仍可在
`runtime/src/companion_runtime/config.py` 查看；新增外部字段必须先归入上述类别，不能无说明地暴露。

### 浪潮与主动发送

新部署默认 fail-closed：shadow 可以观察，live 不会自行发送。

```toml
[langchao]
shadow_enabled = true
social_enabled = true
live_enabled = false
live_scope_allowlist = []
```

要允许主动发送，必须同时满足：

1. `langchao.live_enabled = true`；
2. 当前会话完整字符串出现在 `live_scope_allowlist`；
3. 数据库中的发送权威指针处于 live。

缺任何一项都表现为“服务健康但不主动”。用以下工具逐项诊断：

```bash
docker exec xxj-runtime-fleet python /fleet/fleet_admin.py check <完整会话 scope>
```

### 夜间策略

```toml
[langchao]
night_send_penalty = 0.85
night_send_penalty_start_hour = 0
night_send_penalty_end_hour = 5

[scheduler]
# 完全禁止调度时才设置：
# quiet_hours_start = 0
# quiet_hours_end = 7
```

夜间 penalty 是“提高开口成本”，不是硬停；窗口采用 `[start, end)`。`quiet_hours` 才会让调度器完全不唤醒。

## 多人部署：fleet

一个 Runtime 承载一个角色和一份状态。多人必须使用多个 Runtime 进程与多个 PostgreSQL schema，不能让不同用户共用一个 schema。

生成 fleet：

```bash
python3 scripts/build_runtime_fleet.py \
  --people 10001,10002 \
  --runtime-config config/runtime.toml \
  --project <compose-project>
```

公共 TOML 向所有子进程提供行为、语义和提示词；supervisor 通过更高优先级的环境变量分别覆盖：

- `conversation_id`；
- `runtime_id`；
- PostgreSQL schema；
- 数据目录与端口。

常用操作：

```bash
# 看实例、路由、健康和发送条件
docker exec xxj-runtime-fleet python /fleet/fleet_admin.py list

# 开通并验证一个会话：建 schema、迁移、注册、等健康
docker exec xxj-runtime-fleet python /fleet/fleet_admin.py add <完整会话 scope>

# 解释为什么不会主动发送
docker exec xxj-runtime-fleet python /fleet/fleet_admin.py check <完整会话 scope>

# 默认只下线，不删 schema/data
docker exec xxj-runtime-fleet python /fleet/fleet_admin.py remove <slug>
```

会话到 slug/schema 的规则只有一份：`scripts/fleet_naming.py`。完整首次部署、滚动升级和 PostgreSQL 备份/回滚见 [`docs/部署与运维.md`](docs/部署与运维.md)。

## 健康与排错

```bash
curl -s http://127.0.0.1:8787/health | python3 -m json.tool
python3 scripts/check_versions.py
```

`/health` 中：

- `runtime_version`：正在运行的包版本；
- `state_version`：状态计数器，不是包版本；
- `semantic_provider.available`：语义端点是否真的可用；
- `semantics.unresolved`：等待以后解释的事件数量；增长不等于事件丢失。

常见问题：

| 现象 | 首先检查 |
|---|---|
| `cognition: unavailable` | AstrBot 插件的 `runtime_base_url` 与网络可达性 |
| Runtime 启动即退出 | `.env` 中 PostgreSQL DSN、数据库网络、schema 权限 |
| 服务健康但不主动 | live 开关、精确白名单、持久化权威指针、quiet hours |
| 语义 `available: false` | TOML 的 base URL/model 与环境中的 API key |
| TOML 列表不生效 | TOML 必须用 `[...]`；环境变量覆盖列表必须是 JSON 数组 |

## 数据与隐私

- PostgreSQL 是权威状态；`/data/raw_events.jsonl` 只是可选审计镜像；
- 完整 context 文本默认不落库（`observability.record_context_text = false`）；
- API key 不进入配置对象；PostgreSQL DSN 在 CLI `config` 与 `GET /config` 中也会被替换为 `***redacted***`；
- provider 健康信息只报告 key 的 `configured / not configured`；
- 公开仓库只使用合成用户、文档地址与可移植路径，不包含私人对话、用户画像、健康信息或真实 roster；
- `runtime/tests/test_public_tree_is_sanitized.py` 扫描所有 tracked 文本，阻止隐私与机器标记进入公开树。

需要备份 PostgreSQL 时使用 `pg_dump`、WAL 归档、`pg_basebackup` 与恢复演练。Runtime 的 `checkpoint / backup / restore / verify` 是旧 SQLite 导入兼容接口，在 PostgreSQL 上会明确拒绝。

## 测试

无需数据库的配置、provider、prompt、compose、fleet 和隐私门禁：

```bash
python -m pytest -q \
  runtime/tests/test_core_infrastructure.py \
  runtime/tests/test_providers.py \
  runtime/tests/test_delivery_scheduler_context.py \
  runtime/tests/test_ops_scripts_are_portable.py \
  runtime/tests/test_public_tree_is_sanitized.py \
  runtime/tests/test_personal_compose_config.py \
  runtime/tests/test_fleet_compose_keeps_the_semantic_key.py \
  runtime/tests/test_runtime_fleet_common_config.py
```

PostgreSQL 专项测试需要独立测试数据库：

```bash
CR_TEST_PG_DSN='postgresql://...' python -m pytest -q \
  runtime/tests/test_live_dispatch_legacy_postgres.py \
  runtime/tests/test_langchao_prelaunch_postgres_audit.py \
  runtime/tests/test_langchao_repairs_postgres.py \
  runtime/tests/test_transaction_ownership_postgres.py
```

完整测试集里仍有一批直接构造 SQLite 风格 Runtime 的夹具，尚未迁移到 PostgreSQL；它们不属于上面的发布门禁。具体机制测试与 API 说明见 [`runtime/README.md`](runtime/README.md)。

## 仓库结构

```text
.
├── config/runtime.example.toml    # 个人部署非秘密配置模板
├── docker-compose.yml             # Runtime-only 单人 compose
├── runtime/                       # Runtime 包、机制文档和测试
├── scripts/                       # 当前运维工具与脱敏后的通用回放/仿真工具
├── framework/                     # 可控时钟与 OpenAI-compatible 测试框架
├── archive/                       # 已退出运行路径的本地模型实验归档
├── docs/部署与运维.md
├── CHANGELOG.md
└── LICENSE
```

## 当前限制

- Runtime 不是公网服务，HTTP API 本身没有面向互联网的鉴权层；
- 多人部署不是单进程多租户，而是每人一个进程/schema；
- 记忆检索当前主要是词法路径，没有 embedding 召回；
- 渠道“实际发出”和 Runtime 收到回执之间存在不可消除的崩溃窗口，极端情况下可能重投；
- 插件尚未进入 AstrBot 插件市场时，需要通过 AstrBot 使用 release zip / URL 安装；
- fleet 仍要求部署者提供 PostgreSQL、AstrBot 与 OneBot 的基础服务定义。

## 版本与许可证

当前版本号由以下位置共同校验：

- `CHANGELOG.md`；
- `runtime/pyproject.toml`；
- `runtime/src/companion_runtime/__init__.py`；
- 插件仓库 `metadata.yaml`（存在本地插件仓库时）。

运行 `python3 scripts/check_versions.py` 检查一致性。

许可证：**AGPL-3.0-or-later**，全文见 [`LICENSE`](LICENSE)。需要闭源集成时按许可证中的商业许可说明联系维护者。
