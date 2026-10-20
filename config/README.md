# Runtime 配置分层

个人部署复制 `runtime.example.toml` 为 `runtime.toml`。Runtime 在仓库根目录启动时自动发现该文件；
显式 `--config <path>` 优先，且显式路径缺失会直接报错。

加载顺序：

```text
代码默认值 < runtime.toml < CR_ 环境变量 < CLI host/port/log-level 等少量参数
```

## 1. 必须或条件必填

| 配置 | 条件 | 放哪里 |
|---|---|---|
| `CR_STORAGE__DSN` | 所有生产 Runtime 必须 | `.env` / secret store |
| `conversation_id` | 多会话/fleet 必须是完整 unified_msg_origin | TOML 或 supervisor env |
| `storage.schema` | 多人必须每人唯一 | TOML 或 supervisor env |
| `semantic.base_url` + `semantic.model` + `CR_SEMANTIC_API_KEY` | 需要远端语义时 | 前两项 TOML，key 在 `.env` |

## 2. 推荐个人自定义

- `[persona]`：完整 Runtime 人格；人物层可以完全重写；
- `[values]`：8 个 0–1 价值观轴；
- `[prompts]` 与 `[semantic.prompts]`：四阶段局部细化；
- `[langchao]` 的 shadow/live/social、精确白名单、cold-start 与夜间策略；
- `[scheduler]` 的 quiet hours；
- `drive.max_contacts_per_day`：每日主动预算；
- `candidate.memory_callback_cooldown_hours`：同一记忆再次作为开场的冷却；
- `observability.record_context_text`：仅排查提示词时短期开启，可能保存用户内容。

Persona 不覆盖协议层：边界、权限、时间、防复读、输出格式、来源 grounding 与防编造由代码强制。
AstrBot 的主 Persona 仍在 AstrBot 配置；两份 Persona 应表达同一个人物。

## 3. 运维按需调整

- `server.host / port / log_level`（不要把无公网鉴权的 API 暴露到互联网）；
- `scheduler.min_interval_seconds / max_interval_seconds`；
- 语义 timeout/max_tokens/json_mode 与 deep-refresh 频率；
- raw-event mirror 与 observability；
- fleet identity/schema/path/port 覆盖。

## 4. 校准内核：不建议个人调整

这些字段可以通过 TOML/`CR_` 覆盖，但相互耦合，单改一项常会得到“永远不主动”或过度发送：

- `emotion.*`；
- `drive.*`（除每日主动预算）；
- `silence.*`；
- `utility.*`；
- `memory.*` 的激活/衰减/重要性系数；
- `user_model.*` 的学习率、遗忘率、归因权重；
- `candidate.*` 的 bias、TTL、刷新阈值；
- `langchao.leak / competition_gain / decision_threshold / time_scale_seconds / max_step_seconds`；
- outbox lease/attempt/batch 与 action expiry。

需要调整时应同步写测试、仿真和回滚点，而不是把它当 Persona 参数。

## 5. 不属于个人配置

- legacy SQLite/import/maintenance 字段；
- simulation 与 acceptance-only 开关；
- 内部实验开关、`extras`；
- 隐私删除 bearer digest；
- 数据库口令、API key、token。

其中下面几个字段**已在配置层声明，但目前没有任何生产代码读取**，写进 TOML 不会改变行为，请不要
把它们当开关用：

| 字段 | 现状 |
|---|---|
| `storage.durability_gap_acknowledged` | 仅被测试断言默认值；PostgreSQL 耐久性不再靠它声明 |
| `storage.legacy_sqlite_path` | 只参与路径规范化校验，权威存储只走 DSN |
| `langchao.external_exploration_enabled` | 仅被测试断言默认值；外部探索未实现 |
| `v2_simulation_fit_immediately` | 仅用于离线拟合脚本，生产不读 |

这些属于"要删而不是要配"的一类；需要清理时应连同断言一起删，而不是在文档里保留悬念。

## 6. 价值观生效范围

`[values]` 会在新实例首次创建 `runtime_state` 时播种，并持久化到 PostgreSQL。已有实例不会因为
修改 TOML 而静默换人格；需要显式迁移/管理操作。所有轴必须在 `0.0–1.0`，越界会拒绝启动。
